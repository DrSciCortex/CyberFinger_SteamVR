/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// CfDriver.h — the CyberFinger driver inside WiVRn: both hands, the UDP link
// to the bridge and the IMU fusion (what src/ServerProvider is for SteamVR)
//
//   bridge → driver   CFG2 buttons, CFIM joint IMU        UDP 127.0.0.1:27015
//   driver → bridge   CFHP haptics, CFOP status + hands   UDP 127.0.0.1:27016
//   driver → Resonite FluxActions (MoreFluxActions mod)   UDP 127.0.0.1:42042
//
// One thread of its own, every 5 ms: the buttons' state machines, the gestures,
// the optical observations into the fusion, the context stream, the settings
// file and the saved IMU calibration.
// ═══════════════════════════════════════════════════════════════════════════

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include "CfConfig.h"
#include "CfHand.h"
#include "ImuFusion.h"
#include "StudioLink.h"

namespace cf {

class Driver {
public:
    // configPath: the settings file (DefaultConfigPath()); statePath: where the IMU calibration is kept
    // (DefaultStatePath()). Empty: no file.
    Driver(std::string configPath, std::string statePath);
    ~Driver();
    Driver(const Driver&) = delete;
    Driver& operator=(const Driver&) = delete;

    Hand& GetHand(int hand) { return m_hand[hand & 1]; }
    Config GetConfig() const;
    // Counts the settings being read (from 1): callers that derive something from them know when to do it again.
    uint32_t ConfigGeneration() const { return m_cfgGeneration.load(std::memory_order_acquire); }

    // Whether a CyberFinger holds this hand now: it is switched on and sending, and the headset reports the bare
    // hand (or nothing) there, not a controller the user picked up. The runtime asks with every tracking packet
    // and gives the hand's role to its controller device accordingly.
    bool HoldsRole(int hand, bool handsFree);

    // An application's vibration for this hand, to the CyberFinger's motor (the bridge writes it over BLE).
    void Haptic(int hand, float durationSeconds, float frequency, float amplitude);

private:
    void Run();
    void Tick(double now);
    void PollConfig(bool first);
    void SendFlux(int action, bool pressed);
    void SendContext(const Hand::Snapshot snap[2], double now);
    void LogStatus(double now);
    void LoadImuCalibration();
    void SaveImuCalibration();
    void UpdateImuCalibration(double now);

    const std::string m_configPath, m_statePath;
    mutable std::mutex m_cfgLock;
    Config m_cfg;
    int64_t m_cfgStamp = -1;          // the settings file's modification time as last read (-2: no file)
    std::atomic<uint32_t> m_cfgGeneration{ 0 };
    std::atomic<bool>   m_cfgActive{ true };
    std::atomic<double> m_releaseAfter{ 2.0 };

    StudioLink m_link;
    ImuFusion  m_imu[2];
    Hand       m_hand[2]{ Hand(0), Hand(1) };

    std::thread       m_thread;
    std::atomic<bool> m_run{ false };
    double   m_nextConfigPoll = 0, m_nextContext = 0, m_nextStatus = 0;
    uint32_t m_contextSeq = 0;
    intptr_t m_fluxSocket = -1;
    bool     m_loggedHaptic[2] = {};

    // The IMU calibration carried across sessions (src/ServerProvider.cpp has the same for SteamVR).
    ImuFusion::Calibration m_imuSaved[2];
    bool   m_imuHaveSaved[2] = {};
    ImuFusion::Calibration m_imuBase[2];  // this session's saved start, which new solves are averaged into
    bool   m_imuHaveBase[2] = {};
    size_t m_imuSavedSolves[2] = {};
    size_t m_imuResyncsLogged[2] = {};
    int    m_resyncSeen[2] = { -1, -1 };
    bool   m_imuCalDirty = false;
    double m_nextImuCalCheck = 0, m_nextImuCalSave = 0;
};

// ~/.local/state/cyberfinger (XDG_STATE_HOME honoured).
std::string DefaultStatePath();

} // namespace cf
