/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// HandTrackingReceiver.cpp — UDP listener for hand tracking skeleton data
// ═══════════════════════════════════════════════════════════════════════════

#include "HandTrackingReceiver.h"
#include "Utils.h"

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  pragma comment(lib, "ws2_32.lib")
   typedef int socklen_t;
#  define CLOSE_SOCKET closesocket
#  define INVALID_SOCK INVALID_SOCKET
#else
#  include <sys/socket.h>
#  include <netinet/in.h>
#  include <arpa/inet.h>
#  include <unistd.h>
#  include <fcntl.h>
#  include <errno.h>
#  define CLOSE_SOCKET close
#  define INVALID_SOCK (-1)
   typedef int SOCKET;
#endif

namespace merged_ctrl {

HandTrackingReceiver::HandTrackingReceiver() {}

HandTrackingReceiver::~HandTrackingReceiver() {
    Stop();
}

void HandTrackingReceiver::Start(int udpPort) {
    m_port = udpPort;
    m_running = true;

#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif

    m_thread = std::thread(&HandTrackingReceiver::RecvThread, this);
    DriverLog("HandTrackingReceiver: Listening on UDP port %d\n", m_port);
}

void HandTrackingReceiver::Stop() {
    m_running = false;

    // Close socket to unblock recvfrom
#ifdef _WIN32
    if (m_socket != ~0ULL) {
        CLOSE_SOCKET((SOCKET)m_socket);
        m_socket = ~0ULL;
    }
#else
    if (m_socket >= 0) {
        CLOSE_SOCKET(m_socket);
        m_socket = -1;
    }
#endif

    if (m_thread.joinable()) m_thread.join();

#ifdef _WIN32
    WSACleanup();
#endif
}

void HandTrackingReceiver::RecvThread() {
    SOCKET sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock == INVALID_SOCK) {
        DriverLog("HandTrackingReceiver: Failed to create socket\n");
        return;
    }

    // Allow multiple listeners (for debugging)
    int reuse = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, (const char*)&reuse, sizeof(reuse));

    // Set receive timeout so we can check m_running periodically
#ifdef _WIN32
    DWORD timeout = 100; // ms
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char*)&timeout, sizeof(timeout));
#else
    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 100000; // 100ms
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
#endif

    struct sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(m_port);
    addr.sin_addr.s_addr = INADDR_ANY;

    if (bind(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        DriverLog("HandTrackingReceiver: Failed to bind port %d\n", m_port);
        CLOSE_SOCKET(sock);
        return;
    }

#ifdef _WIN32
    m_socket = (uintptr_t)sock;
#else
    m_socket = sock;
#endif

    DriverLog("HandTrackingReceiver: Socket bound, listening...\n");

    char buf[sizeof(HandTrackingPacket) + 64];

    while (m_running) {
        struct sockaddr_in src{};
        socklen_t srcLen = sizeof(src);

        int n = recvfrom(sock, buf, sizeof(buf), 0,
                         (struct sockaddr*)&src, &srcLen);

        if (n < 4) continue; // need at least the magic field

        uint32_t magic = *reinterpret_cast<const uint32_t*>(buf);

        if (magic == kHandTrackingMagic && n >= (int)sizeof(HandTrackingPacket)) {
            auto* pkt = reinterpret_cast<const HandTrackingPacket*>(buf);
            if (pkt->version == kHandTrackingVersion)
                ProcessPacket(*pkt);
        }
        else if (magic == kGamepadMagic && n >= (int)sizeof(GamepadPacket)) {
            auto* pkt = reinterpret_cast<const GamepadPacket*>(buf);
            ProcessGamepad(*pkt);
        }
    }

    CLOSE_SOCKET(sock);
}

void HandTrackingReceiver::ProcessPacket(const HandTrackingPacket& pkt) {
    HandTrackingState state;
    state.valid = true;
    state.timestamp = NowSeconds();
    state.confidence = pkt.confidence / 255.f;

    // Log first packet per hand
    static bool firstHT[2] = {false, false};
    int h = (pkt.hand == 0) ? 0 : 1;
    if (!firstHT[h]) {
        firstHT[h] = true;
        DriverLog("HandTrackingReceiver: First HT packet for %s hand! pos=(%.3f,%.3f,%.3f) conf=%d\n",
                  h == 0 ? "LEFT" : "RIGHT", pkt.pos[0], pkt.pos[1], pkt.pos[2], pkt.confidence);
    }

    state.pos[0] = pkt.pos[0];
    state.pos[1] = pkt.pos[1];
    state.pos[2] = pkt.pos[2];

    state.quat[0] = pkt.quat[0]; // w
    state.quat[1] = pkt.quat[1]; // x
    state.quat[2] = pkt.quat[2]; // y
    state.quat[3] = pkt.quat[3]; // z

    // Unpack per-bone transforms
    for (int i = 0; i < kNumBones; ++i) {
        auto& bone = state.bones[i];
        bone.position.v[0] = pkt.bones[i][0];
        bone.position.v[1] = pkt.bones[i][1];
        bone.position.v[2] = pkt.bones[i][2];
        bone.position.v[3] = 1.f; // w component of position (unused but set)
        bone.orientation.w  = pkt.bones[i][3];
        bone.orientation.x  = pkt.bones[i][4];
        bone.orientation.y  = pkt.bones[i][5];
        bone.orientation.z  = pkt.bones[i][6];
    }

    // Copy curl values
    for (int i = 0; i < 5; ++i)
        state.curls[i] = pkt.curls[i];

    std::lock_guard<std::mutex> lock(m_mutex);
    if (pkt.hand == 0)
        m_left = state;
    else
        m_right = state;
}

HandTrackingState HandTrackingReceiver::GetLeft() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_left;
}

HandTrackingState HandTrackingReceiver::GetRight() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_right;
}

bool HandTrackingReceiver::HasRecentData(int hand, double maxAge) const {
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto& state = (hand == 0) ? m_left : m_right;
    if (!state.valid) return false;
    return (NowSeconds() - state.timestamp) < maxAge;
}

// ── Gamepad support ────────────────────────────────────────────────────────

void HandTrackingReceiver::ProcessGamepad(const GamepadPacket& pkt) {
    GamepadState state;
    state.valid = true;
    state.timestamp = NowSeconds();
    state.buttons = pkt.buttons;
    state.joy_x = pkt.joy_x;
    state.joy_y = pkt.joy_y;
    state.trigger_analog = pkt.trigger_analog;
    state.battery_pct = pkt.battery_pct;

    // Log first packet per hand
    static bool firstLog[2] = {false, false};
    int h = (pkt.hand == 0) ? 0 : 1;
    if (!firstLog[h]) {
        firstLog[h] = true;
        DriverLog("HandTrackingReceiver: First gamepad packet for %s hand! btn=0x%02X jX=%d jY=%d\n",
                  h == 0 ? "LEFT" : "RIGHT", pkt.buttons, pkt.joy_x, pkt.joy_y);
    }

    std::lock_guard<std::mutex> lock(m_mutex);
    if (pkt.hand == 0)
        m_gamepadLeft = state;
    else
        m_gamepadRight = state;
}

GamepadState HandTrackingReceiver::GetGamepadLeft() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_gamepadLeft;
}

GamepadState HandTrackingReceiver::GetGamepadRight() const {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_gamepadRight;
}

bool HandTrackingReceiver::HasRecentGamepad(int hand, double maxAge) const {
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto& state = (hand == 0) ? m_gamepadLeft : m_gamepadRight;
    if (!state.valid) return false;
    return (NowSeconds() - state.timestamp) < maxAge;
}

} // namespace merged_ctrl

