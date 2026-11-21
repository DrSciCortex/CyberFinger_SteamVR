/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// StudioLink.h — UDP link to the bridge (CyberFinger GUI)
//
// Receives CyberFinger input (CFG2, legacy CFGP), fused hand state (CFHS) and raw
// CyberFinger IMU data (CFIM, during captures) on the driver port; sends the context
// stream (CFOP) to the bridge's port.
// ═══════════════════════════════════════════════════════════════════════════

#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <thread>
#include "Protocol.h"

namespace cf {

struct CyberFingerState {
    bool     valid = false;
    double   time = -1e9;         // arrival, NowSeconds()
    uint8_t  buttons = 0;         // CyberFingerButton bits (current firmware layout)
    uint8_t  buttons2 = 0;        // CyberFingerButton2 bits (the pink button; CFG2 only)
    uint8_t  resync = 0;          // the bridge's IMU fusion resync count: a new value asks for ImuFusion::Resync
    bool     triggerAnalog = false;
    float    trigger = 0.f;       // 0..1, when triggerAnalog
    float    joyX = 0.f, joyY = 0.f;   // -1..1, +y up
    uint8_t  battery = 100;
};

struct HandStateSample {
    bool   valid = false;
    double arrival = -1e9;        // NowSeconds()
    HandStatePacket pkt{};
};

class StudioLink {
public:
    StudioLink();
    ~StudioLink();

    // Called on the receive thread for every CFIM packet, with its arrival time. Set before Start().
    using ImuSink = std::function<void(const ImuPacket&, double arrival)>;
    void SetImuSink(ImuSink sink) { m_imuSink = std::move(sink); }

    bool Start(int listenPort, int contextPort, bool loopbackOnly, bool legacy5bit);
    void Stop();

    CyberFingerState CyberFinger(int hand) const;
    HandStateSample HandState(int hand) const;
    void SendContext(const ContextPacket& pkt);
    void SendHaptic(int hand, float durationSeconds, float frequency, float amplitude);

    uint64_t CyberFingerPackets() const { return m_cyberFingerPackets.load(); }
    uint64_t HandStatePackets() const { return m_handStatePackets.load(); }

private:
    void RecvThread();
    void OnDatagram(const uint8_t* data, int size);

    std::thread       m_thread;
    std::atomic<bool> m_running{ false };
    int  m_listenPort = 27015;
    int  m_contextPort = 27016;
    bool m_loopbackOnly = true;
    bool m_legacy5bit = false;

    intptr_t m_sendSocket = -1;
    std::mutex m_sendLock;
    uint32_t m_hapticSeq[2] = {};
    void SendRaw(const void* data, int size);

    mutable std::mutex m_lock;
    CyberFingerState      m_cyberFinger[2];
    HandStateSample m_handState[2];

    ImuSink m_imuSink;

    std::atomic<uint64_t> m_cyberFingerPackets{ 0 };
    std::atomic<uint64_t> m_handStatePackets{ 0 };
    bool m_loggedFirst[4][2] = {};
};

} // namespace cf
