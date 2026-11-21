/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// StudioLink.cpp — UDP receive thread + context sender
// ═══════════════════════════════════════════════════════════════════════════

#include "StudioLink.h"
#include "Utils.h"
#include <cmath>
#include <cstring>

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  include <mstcpip.h>
#  pragma comment(lib, "ws2_32.lib")
#  ifndef SIO_UDP_CONNRESET
#    define SIO_UDP_CONNRESET _WSAIOW(IOC_VENDOR, 12)
#  endif
   typedef int socklen_t;
#  define CLOSE_SOCKET closesocket
#else
#  include <arpa/inet.h>
#  include <fcntl.h>
#  include <netinet/in.h>
#  include <sys/socket.h>
#  include <unistd.h>
#  define CLOSE_SOCKET close
   typedef int SOCKET;
#  define INVALID_SOCKET (-1)
#endif

namespace cf {

namespace {

constexpr intptr_t kNoSocket = -1;

#ifdef _WIN32
// A send to a closed UDP port makes Windows fail the next recvfrom() on that
// socket with WSAECONNRESET; switch that behaviour off.
void DisableConnReset(SOCKET s) {
    BOOL off = FALSE;
    DWORD ret = 0;
    WSAIoctl(s, SIO_UDP_CONNRESET, &off, sizeof(off), nullptr, 0, &ret, nullptr, nullptr);
}
#endif

// Old (pre-2026) firmware layout: bit0 trigger, bit1 grip, bit2 B, bit3 stick click, bit4 A.
// Mapped onto the buttons that drive /input/a (Start/Select) and /input/b (MENU) by default.
uint8_t FromLegacy5Bit(uint8_t b) {
    uint8_t out = 0;
    if (b & 0x01) out |= kBtnTrigger;
    if (b & 0x02) out |= kBtnGrip;
    if (b & 0x04) out |= kBtnMenu;          // B
    if (b & 0x08) out |= kBtnStickClick;
    if (b & 0x10) out |= kBtnStartSelect;   // A
    return out;
}

void RadialDeadzone(float& x, float& y, float dz) {
    const float m = std::sqrt(x * x + y * y);
    if (m <= dz) { x = y = 0.f; return; }
    const float s = std::fmin(1.f, (m - dz) / (1.f - dz)) / m;
    x *= s;
    y *= s;
}

float Clamp11(float v) { return v < -1.f ? -1.f : (v > 1.f ? 1.f : v); }

} // namespace

StudioLink::StudioLink() = default;

StudioLink::~StudioLink() { Stop(); }

bool StudioLink::Start(int listenPort, int contextPort, bool loopbackOnly, bool legacy5bit) {
    m_listenPort = listenPort;
    m_contextPort = contextPort;
    m_loopbackOnly = loopbackOnly;
    m_legacy5bit = legacy5bit;

#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif

    SOCKET send = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (send != INVALID_SOCKET) {
#ifdef _WIN32
        DisableConnReset(send);
        u_long nonBlocking = 1;
        ioctlsocket(send, FIONBIO, &nonBlocking);
#else
        fcntl(send, F_SETFL, fcntl(send, F_GETFL, 0) | O_NONBLOCK);
#endif
        m_sendSocket = intptr_t(send);
    }

    m_running = true;
    m_thread = std::thread(&StudioLink::RecvThread, this);
    DriverLog("StudioLink: listening on UDP %s:%d, context stream to 127.0.0.1:%d\n",
              loopbackOnly ? "127.0.0.1" : "0.0.0.0", listenPort, contextPort);
    return true;
}

void StudioLink::Stop() {
    if (!m_running.exchange(false)) return;
    if (m_thread.joinable()) m_thread.join();   // recvfrom times out every 100 ms
    {
        std::lock_guard<std::mutex> g(m_sendLock);
        if (m_sendSocket != kNoSocket) CLOSE_SOCKET(SOCKET(m_sendSocket));
        m_sendSocket = kNoSocket;
    }
#ifdef _WIN32
    WSACleanup();
#endif
}

void StudioLink::RecvThread() {
    SOCKET sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock == INVALID_SOCKET) {
        DriverLog("StudioLink: failed to create socket\n");
        return;
    }
#ifdef _WIN32
    DisableConnReset(sock);
    DWORD timeout = 100;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
#else
    timeval tv{ 0, 100000 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
#endif
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(uint16_t(m_listenPort));
    addr.sin_addr.s_addr = htonl(m_loopbackOnly ? INADDR_LOOPBACK : INADDR_ANY);
    if (bind(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        DriverLog("StudioLink: failed to bind UDP port %d (another program using it?)\n", m_listenPort);
        CLOSE_SOCKET(sock);
        return;
    }

    alignas(8) uint8_t buf[4096];
    while (m_running) {
        sockaddr_in src{};
        socklen_t srcLen = sizeof(src);
        const int n = recvfrom(sock, reinterpret_cast<char*>(buf), sizeof(buf), 0,
                               reinterpret_cast<sockaddr*>(&src), &srcLen);
        if (n >= 4) OnDatagram(buf, n);
    }
    CLOSE_SOCKET(sock);
}

void StudioLink::OnDatagram(const uint8_t* data, int size) {
    uint32_t magic;
    std::memcpy(&magic, data, sizeof(magic));
    const double now = NowSeconds();

    if (magic == kMagicCyberFinger && size >= int(sizeof(CyberFingerPacket))) {
        CyberFingerPacket p;
        std::memcpy(&p, data, sizeof(p));
        if (p.h.version != kVersion || p.h.hand > 1) return;
        CyberFingerState g;
        g.valid = true;
        g.time = now;
        g.buttons = p.buttons;
        g.buttons2 = p.buttons2;
        g.resync = p.resync;
        g.triggerAnalog = true;
        g.trigger = p.trigger / 255.f;
        g.joyX = Clamp11(p.joy_x / 32767.f);
        g.joyY = Clamp11(p.joy_y / 32767.f);
        g.battery = p.battery_pct;
        {
            std::lock_guard<std::mutex> lk(m_lock);
            m_cyberFinger[p.h.hand] = g;
        }
        ++m_cyberFingerPackets;
        if (!m_loggedFirst[0][p.h.hand]) {
            m_loggedFirst[0][p.h.hand] = true;
            DriverLog("StudioLink: first CyberFinger packet (CFG2) for %s hand\n", p.h.hand ? "right" : "left");
        }
    } else if (magic == kMagicLegacy && size >= int(sizeof(LegacyCyberFingerPacket))) {
        LegacyCyberFingerPacket p;
        std::memcpy(&p, data, sizeof(p));
        if (p.hand > 1) return;
        CyberFingerState g;
        g.valid = true;
        g.time = now;
        g.buttons = m_legacy5bit ? FromLegacy5Bit(p.buttons) : p.buttons;
        g.triggerAnalog = p.trigger_analog > 10;   // older firmware sends 0 with a digital trigger
        g.trigger = p.trigger_analog / 255.f;
        g.joyX = Clamp11(p.joy_x / 32767.f);
        g.joyY = Clamp11(-p.joy_y / 32767.f);      // legacy sends +y = down
        RadialDeadzone(g.joyX, g.joyY, 0.12f);     // uncentred stick: CFG2 senders centre it themselves
        g.battery = p.battery_pct;
        {
            std::lock_guard<std::mutex> lk(m_lock);
            m_cyberFinger[p.hand] = g;
        }
        ++m_cyberFingerPackets;
        if (!m_loggedFirst[1][p.hand]) {
            m_loggedFirst[1][p.hand] = true;
            DriverLog("StudioLink: first legacy CyberFinger packet (CFGP) for %s hand\n", p.hand ? "right" : "left");
        }
    } else if (magic == kMagicHandState && size >= int(sizeof(HandStatePacket))) {
        HandStateSample s;
        std::memcpy(&s.pkt, data, sizeof(s.pkt));
        if (s.pkt.h.version != kVersion || s.pkt.h.hand > 1) return;
        s.valid = true;
        s.arrival = now;
        const int hand = s.pkt.h.hand;
        {
            std::lock_guard<std::mutex> lk(m_lock);
            m_handState[hand] = s;
        }
        ++m_handStatePackets;
        if (!m_loggedFirst[2][hand]) {
            m_loggedFirst[2][hand] = true;
            DriverLog("StudioLink: first fused hand state (CFHS) for %s hand\n", hand ? "right" : "left");
        }
    } else if (magic == kMagicImu && size >= int(sizeof(ImuPacket))) {
        ImuPacket p;
        std::memcpy(&p, data, sizeof(p));
        if (p.h.version != kVersion || p.h.hand > 1) return;
        if (m_imuSink) m_imuSink(p, now);
        if (!m_loggedFirst[3][p.h.hand]) {
            m_loggedFirst[3][p.h.hand] = true;
            DriverLog("StudioLink: first CyberFinger IMU packet (CFIM) for %s hand, slots 0x%x\n",
                      p.h.hand ? "right" : "left", unsigned(p.present));
        }
    }
}

CyberFingerState StudioLink::CyberFinger(int hand) const {
    std::lock_guard<std::mutex> lk(m_lock);
    return m_cyberFinger[hand & 1];
}

HandStateSample StudioLink::HandState(int hand) const {
    std::lock_guard<std::mutex> lk(m_lock);
    return m_handState[hand & 1];
}

void StudioLink::SendRaw(const void* data, int size) {
    std::lock_guard<std::mutex> g(m_sendLock);
    if (m_sendSocket == kNoSocket) return;
    sockaddr_in dst{};
    dst.sin_family = AF_INET;
    dst.sin_port = htons(uint16_t(m_contextPort));
    dst.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sendto(SOCKET(m_sendSocket), reinterpret_cast<const char*>(data), size, 0,
           reinterpret_cast<const sockaddr*>(&dst), sizeof(dst));
}

void StudioLink::SendContext(const ContextPacket& pkt) { SendRaw(&pkt, int(sizeof(pkt))); }

void StudioLink::SendHaptic(int hand, float durationSeconds, float frequency, float amplitude) {
    HapticPacket p{};
    p.h.magic = kMagicHaptic;
    p.h.version = kVersion;
    p.h.hand = uint8_t(hand & 1);
    p.h.seq = ++m_hapticSeq[hand & 1];
    p.h.t_send_us = NowMicros();
    p.duration_s = durationSeconds;
    p.frequency_hz = frequency;
    p.amplitude = amplitude;
    SendRaw(&p, int(sizeof(p)));
}

} // namespace cf
