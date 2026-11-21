/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// CfDriver.cpp
// ═══════════════════════════════════════════════════════════════════════════

#include "CfDriver.h"
#include <arpa/inet.h>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <netinet/in.h>
#include <sstream>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#include "TrackingTrust.h"
#include "Utils.h"

namespace cf {

namespace {

constexpr double kTickSeconds = 0.005;
constexpr double kContextInterval = 1.0 / 100.0;
constexpr double kStatusInterval = 10.0;
constexpr double kSaveMaxResidualDeg = 15.0;   // only a good full solve is saved
constexpr double kAverageWithinDeg = 25.0;      // a solve this close to the saved one is averaged with it
constexpr const char* kImuSource = "wivrn";     // the hand-tracking source the saved mounting belongs to

const char* HandName(int hand) { return hand ? "right" : "left"; }

const char* ModeName(uint8_t m) {
    switch (m) {
        case kModeFused: return "FUSED";
        case kModePassthrough: return "PASSTHROUGH";
        case kModeNoPose: return "NO_POSE";
        case kModeReleased: return "RELEASED";
        default: return "NONE";
    }
}

double QuatAngleDeg(const Quat& a, const Quat& b) {
    const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
    return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
}

void ToFloats(const Xform& x, float pos[3], float rot[4]) {
    pos[0] = float(x.p.x); pos[1] = float(x.p.y); pos[2] = float(x.p.z);
    rot[0] = float(x.q.w); rot[1] = float(x.q.x); rot[2] = float(x.q.y); rot[3] = float(x.q.z);
}

void ToFloats(const Vec3& v, float out[3]) {
    out[0] = float(v.x); out[1] = float(v.y); out[2] = float(v.z);
}

// The file's modification time in ns, -2 if there is none.
int64_t FileStamp(const std::string& path) {
    struct stat st{};
    if (path.empty() || ::stat(path.c_str(), &st) != 0) return -2;
    return int64_t(st.st_mtim.tv_sec) * 1000000000 + st.st_mtim.tv_nsec;
}

} // namespace

std::string DefaultStatePath() {
    const char* xdg = std::getenv("XDG_STATE_HOME");
    if (xdg && *xdg) return std::string(xdg) + "/cyberfinger";
    const char* home = std::getenv("HOME");
    return home && *home ? std::string(home) + "/.local/state/cyberfinger" : std::string();
}

Driver::Driver(std::string configPath, std::string statePath)
    : m_configPath(std::move(configPath)), m_statePath(std::move(statePath)) {
    PollConfig(true);
    for (int hand = 0; hand < 2; ++hand) {
        m_imu[hand].Reset();
        m_hand[hand].SetImuFusion(&m_imu[hand]);
    }
    LoadImuCalibration();

    // The joint IMU, stamped when its report reached the bridge over BLE (the bridge's clock is ours: both
    // CLOCK_MONOTONIC; if it ever isn't, the arrival here).
    m_link.SetImuSink([this](const ImuPacket& p, double arrival) {
        if (!(p.present & kImuJoint)) return;
        double t = (double(p.h.t_send_us) - double(p.h.age_us)) * 1e-6;
        if (std::fabs(arrival - t) > 0.5) t = arrival;
        m_imu[p.h.hand & 1].AddImu(t, Quat{ p.quat[2][0], p.quat[2][1], p.quat[2][2], p.quat[2][3] });
    });
    const Config cfg = GetConfig();
    m_link.Start(cfg.listenPort, cfg.contextPort, cfg.loopbackOnly, cfg.legacy5bit);

    const int s = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s >= 0) {
        ::fcntl(s, F_SETFL, ::fcntl(s, F_GETFL, 0) | O_NONBLOCK);
        m_fluxSocket = s;
    }

    m_run = true;
    m_thread = std::thread(&Driver::Run, this);
    DriverLog("CyberFinger driver for WiVRn started (%s)\n", cfg.active ? "active" : "switched off in its settings");
}

Driver::~Driver() {
    m_run = false;
    if (m_thread.joinable()) m_thread.join();
    m_link.Stop();
    if (m_imuCalDirty) SaveImuCalibration();
    if (m_fluxSocket >= 0) ::close(int(m_fluxSocket));
}

Config Driver::GetConfig() const {
    std::lock_guard<std::mutex> g(m_cfgLock);
    return m_cfg;
}

bool Driver::HoldsRole(int hand, bool handsFree) {
    hand &= 1;
    const CyberFingerState g = m_link.CyberFinger(hand);
    const bool hold = m_cfgActive.load(std::memory_order_relaxed) && handsFree && g.valid &&
                      NowSeconds() - g.time < m_releaseAfter.load(std::memory_order_relaxed);
    if (hold != m_hand[hand].Active()) {
        m_hand[hand].SetActive(hold);
        DriverLog("[%s] %s\n", HandName(hand),
                  hold ? "CyberFinger takes the hand: its buttons, the headset's hand tracking"
                       : handsFree ? "CyberFinger silent: the hand goes back to WiVRn's hand tracking"
                                   : "a controller was picked up: the hand goes back to it");
    }
    return hold;
}

void Driver::Haptic(int hand, float durationSeconds, float frequency, float amplitude) {
    hand &= 1;
    m_link.SendHaptic(hand, durationSeconds, frequency, amplitude);
    if (!m_loggedHaptic[hand]) {
        m_loggedHaptic[hand] = true;
        DriverLog("[%s] first haptic request (%.3f s, %.0f Hz, amplitude %.2f), sent on to the bridge\n",
                  HandName(hand), durationSeconds, frequency, amplitude);
    }
}

// ── the driver's thread ────────────────────────────────────────────────────

void Driver::Run() {
    auto next = std::chrono::steady_clock::now();
    while (m_run) {
        Tick(NowSeconds());
        next += std::chrono::duration_cast<std::chrono::steady_clock::duration>(
            std::chrono::duration<double>(kTickSeconds));
        const auto now = std::chrono::steady_clock::now();
        if (next < now) next = now;                  // fell behind (suspended): don't catch up in a burst
        std::this_thread::sleep_until(next);
    }
}

void Driver::Tick(double now) {
    if (now >= m_nextConfigPoll) {
        m_nextConfigPoll = now + 1.0;
        PollConfig(false);
    }

    CyberFingerState state[2];
    Hand::Snapshot snap[2];
    for (int hand = 0; hand < 2; ++hand) {
        state[hand] = m_link.CyberFinger(hand);
        snap[hand] = m_hand[hand].Snap(now);
        // The bridge asks for an IMU fusion resync (its button, a triple tap on the CyberFinger) with a new count
        // in CFG2: a lost packet only delays it. The first count seen from a bridge is its start, not a request.
        if (!state[hand].valid) continue;
        if (m_resyncSeen[hand] >= 0 && state[hand].resync != m_resyncSeen[hand]) {
            m_imu[hand].Resync();
            m_imuResyncsLogged[hand] = m_imu[hand].GetStatus().resyncs;
            DriverLog("[%s] IMU fusion resync, asked for by the bridge\n", HandName(hand));
        }
        m_resyncSeen[hand] = state[hand].resync;
    }

    std::vector<FluxEvent> flux;
    for (int hand = 0; hand < 2; ++hand) {
        // How far the headset's tracking of the hand is trusted: where it is seen from, the other hand in front
        // of it (TrackingTrust.h).
        double trust = 1.0;
        if (snap[hand].headValid && snap[hand].gripValid) {
            const Hand::Snapshot& other = snap[1 - hand];
            trust = TrackingTrust(hand, snap[hand].head, snap[hand].grip.pose.p,
                                  other.gripValid ? &other.grip.pose.p : nullptr);
        }
        m_hand[hand].Tick(state[hand], trust, now, flux);
    }
    for (const FluxEvent& e : flux) SendFlux(e.action, e.pressed);

    if (now >= m_nextContext) {
        m_nextContext = now + kContextInterval;
        SendContext(snap, now);
    }
    UpdateImuCalibration(now);
    if (now >= m_nextStatus) {
        m_nextStatus = now + kStatusInterval;
        LogStatus(now);
    }
}

void Driver::PollConfig(bool first) {
    const int64_t stamp = FileStamp(m_configPath);
    if (!first && stamp == m_cfgStamp) return;
    m_cfgStamp = stamp;

    Config cfg;                                      // a key the file drops goes back to its default
    if (stamp == -2) {
        if (first && !m_configPath.empty())
            DriverLog("settings: no %s, using the defaults\n", m_configPath.c_str());
        else if (!first)
            DriverLog("settings: %s is gone, back to the defaults\n", m_configPath.c_str());
    } else {
        std::ifstream f(m_configPath);
        std::stringstream text;
        text << f.rdbuf();
        std::string warnings;
        if (!ParseConfig(text.str(), cfg, warnings)) {
            DriverLog("settings: %s is not a JSON object, keeping the settings as they are\n", m_configPath.c_str());
            if (!first) return;
        } else {
            DriverLog("settings: read %s\n", m_configPath.c_str());
        }
        std::istringstream lines(warnings);
        for (std::string line; std::getline(lines, line);) DriverLog("settings: %s\n", line.c_str());
    }

    {
        std::lock_guard<std::mutex> g(m_cfgLock);
        if (!first && (cfg.listenPort != m_cfg.listenPort || cfg.contextPort != m_cfg.contextPort ||
                       cfg.loopbackOnly != m_cfg.loopbackOnly || cfg.legacy5bit != m_cfg.legacy5bit))
            DriverLog("settings: the UDP ports and the legacy button layout apply when WiVRn starts again\n");
        m_cfg = cfg;
    }
    m_cfgActive = cfg.active;
    m_releaseAfter = cfg.releaseAfter;
    m_cfgGeneration.fetch_add(1, std::memory_order_release);
    for (Hand& h : m_hand) h.SetConfig(cfg);
}

void Driver::SendFlux(int action, bool pressed) {
    const int port = GetConfig().fluxPort;
    if (m_fluxSocket < 0 || port <= 0) return;
    char text[48];
    const int n = std::snprintf(text, sizeof(text), "FluxAction%d.%s", action, pressed ? "Pressed" : "Released");
    sockaddr_in dst{};
    dst.sin_family = AF_INET;
    dst.sin_port = htons(uint16_t(port));
    dst.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::sendto(int(m_fluxSocket), text, size_t(n), 0, reinterpret_cast<const sockaddr*>(&dst), sizeof(dst));
}

// The context stream, as the SteamVR driver sends it: the bridge shows the driver's state from it and draws the
// hands. "Tap" is the headset's hand tracking: the pose is the grip pose, the bones the hand joints
// (BonesFromJoints), all in WiVRn's tracking space.
void Driver::SendContext(const Hand::Snapshot snap[2], double now) {
    ContextPacket pkt{};
    pkt.h.magic = kMagicContext;
    pkt.h.version = kVersion;
    pkt.h.hand = 0xFF;
    pkt.h.seq = ++m_contextSeq;
    pkt.h.t_send_us = NowMicros();

    pkt.hmd_valid = snap[0].headValid || snap[1].headValid;
    ToFloats(snap[0].headValid ? snap[0].head : snap[1].head, pkt.hmd_pos, pkt.hmd_rot);
    pkt.mode_left = m_hand[0].Mode(now);
    pkt.mode_right = m_hand[1].Mode(now);
    pkt.tap_hook_ok = snap[0].jointsValid || snap[1].jointsValid;

    for (int hand = 0; hand < 2; ++hand) {
        const Hand::Snapshot& s = snap[hand];
        TapHand& t = pkt.tap[hand];
        t.pose_valid = s.gripValid;
        t.skel_valid = s.jointsValid;
        t.tracking_result = s.gripValid ? 200 : 201;        // vr::TrackingResult_Running_OK / _OutOfRange
        t.bone_count = s.jointsValid ? uint8_t(eBone_Count) : 0;
        t.source_found = s.grip.orientationValid || s.jointsAge < 1e8;
        t.skel_age_us = uint32_t(std::min(std::max(s.jointsAge, 0.0), 4000.0) * 1e6);
        ToFloats(s.grip.pose, t.raw_pos, t.raw_rot);
        ToFloats(s.grip.linear, t.lin_vel);
        ToFloats(s.grip.angular, t.ang_vel);
        static_assert(sizeof(t.bones) == sizeof(s.bones), "bone layout");
        std::memcpy(t.bones, s.bones, sizeof(t.bones));
    }
    m_link.SendContext(pkt);
}

void Driver::LogStatus(double now) {
    const Config cfg = GetConfig();
    if (!cfg.statusLog) return;
    for (int hand = 0; hand < 2; ++hand) {
        const CyberFingerState g = m_link.CyberFinger(hand);
        if (!g.valid || now - g.time > 60.0) continue;   // no CyberFinger for this hand (any more): nothing to say
        const Hand::Snapshot s = m_hand[hand].Snap(now);
        const ImuFusion::Status is = m_imu[hand].GetStatus();
        char imu[160] = "off";
        if (cfg.imuFusion && is.offHand)
            std::snprintf(imu, sizeof(imu), "the CyberFinger is off the hand");
        else if (cfg.imuFusion && is.calibrated)
            std::snprintf(imu, sizeof(imu), "lag %.0f ms, %s %.1f deg, %zu solves, %zu optical samples refused%s",
                          is.lag * 1e3, is.fromPrior ? "starting mount fits" : "mount fit", is.residualDeg, is.solves,
                          is.rejected, m_hand[hand].ImuFused() ? "" : ", IMU silent");
        else if (cfg.imuFusion && is.priorResidualDeg >= 0)
            std::snprintf(imu, sizeof(imu), "calibrating (%zu pairs; the starting mount is off by %.0f deg)", is.pairs,
                          is.priorResidualDeg);
        else if (cfg.imuFusion)
            std::snprintf(imu, sizeof(imu), "calibrating (%zu pairs)", is.pairs);
        DriverLog("[%s] status: mode=%s CyberFinger=%s hand(pose=%d joints=%d age=%.2fs) imu(%s)\n", HandName(hand),
                  ModeName(m_hand[hand].Mode(now)), now - g.time < 0.5 ? "live" : "stale", int(s.gripValid),
                  int(s.jointsValid), std::min(s.jointsAge, 999.0), imu);
    }
}

// ── IMU calibration across sessions ────────────────────────────────────────
// The joint IMU's mounting belongs to the CyberFinger and how it sits, the lag to the hand-tracking source: both
// carry over, so the fusion starts from them and only fits the IMU's heading (new with every power-up), within a
// second of seeing the hand. Nothing saved yet: the first session waits for a full solve (the SteamVR driver's
// reference mounting is in Steam Link's hand frame, not WiVRn's). The file is plain text, in the SteamVR
// driver's format; deleting it starts afresh.

// Lines: <left|right> <source> <mount w x y z> <lag ms> <fit deg>; '#' comments.
void Driver::LoadImuCalibration() {
    if (m_statePath.empty()) return;
    const std::string path = m_statePath + "/imu_calibration.txt";
    std::ifstream f(path);
    int n = 0;
    for (std::string line; std::getline(f, line);) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream ss(line);
        std::string hand, source;
        ImuFusion::Calibration c;
        double lagMs = 0;
        if (!(ss >> hand >> source >> c.mount.w >> c.mount.x >> c.mount.y >> c.mount.z >> lagMs >> c.residualDeg))
            continue;
        const double norm = std::sqrt(c.mount.w * c.mount.w + c.mount.x * c.mount.x + c.mount.y * c.mount.y +
                                      c.mount.z * c.mount.z);
        if ((hand != "left" && hand != "right") || Lower(source) != kImuSource || norm < 0.5 || norm > 1.5) continue;
        c.mount = Normalize(c.mount);
        c.lag = std::max(0.0, lagMs * 1e-3);
        const int h = hand == "right";
        m_imuSaved[h] = m_imuBase[h] = c;
        m_imuHaveSaved[h] = m_imuHaveBase[h] = true;
        m_imu[h].SetPrior(c);
        ++n;
    }
    if (n) DriverLog("IMU calibration: %d saved (%s): the fusion starts from them\n", n, path.c_str());
}

void Driver::SaveImuCalibration() {
    if (m_statePath.empty()) return;
    std::error_code ec;
    std::filesystem::create_directories(m_statePath, ec);
    const std::string path = m_statePath + "/imu_calibration.txt", tmp = path + ".tmp";
    std::FILE* f = std::fopen(tmp.c_str(), "w");
    if (!f) {
        DriverLog("IMU calibration: cannot write %s\n", tmp.c_str());
        return;
    }
    std::fprintf(f, "# CyberFinger IMU calibration, kept by the driver: how the joint IMU sits on each hand, and\n"
                    "# the hand-tracking source's lag, per hand and source. The IMU fusion starts from it; delete this\n"
                    "# file to start afresh.\n"
                    "# hand source mount_w mount_x mount_y mount_z lag_ms fit_deg\n");
    for (int hand = 0; hand < 2; ++hand) {
        const ImuFusion::Calibration& c = m_imuSaved[hand];
        if (m_imuHaveSaved[hand])
            std::fprintf(f, "%s %s %.5f %.5f %.5f %.5f %.1f %.1f\n", HandName(hand), kImuSource, c.mount.w, c.mount.x,
                         c.mount.y, c.mount.z, c.lag * 1e3, c.residualDeg);
    }
    const bool ok = std::fclose(f) == 0;
    if (ok && std::rename(tmp.c_str(), path.c_str()) == 0) m_imuCalDirty = false;
    if (m_imuCalDirty) DriverLog("IMU calibration: cannot replace %s\n", path.c_str());
}

void Driver::UpdateImuCalibration(double now) {
    if (now < m_nextImuCalCheck) return;
    m_nextImuCalCheck = now + 0.5;
    for (int hand = 0; hand < 2; ++hand) {
        const ImuFusion::Status st = m_imu[hand].GetStatus();
        if (st.resyncs > m_imuResyncsLogged[hand])
            DriverLog("[%s] IMU fusion resync: the joint IMU came back after a gap (the CyberFinger switched off?)\n",
                      HandName(hand));
        m_imuResyncsLogged[hand] = st.resyncs;
        // A new good full solve: remember it. Sessions scatter by ~15° (how the CyberFinger sits, the postures
        // seen), so a solve near the saved calibration is averaged with it rather than replacing it.
        ImuFusion::Calibration c;
        if (st.solves == m_imuSavedSolves[hand] || st.residualDeg > kSaveMaxResidualDeg ||
            !m_imu[hand].GetCalibration(c))
            continue;
        const bool first = m_imuSavedSolves[hand] == 0;
        m_imuSavedSolves[hand] = st.solves;
        const bool average = m_imuHaveBase[hand] &&
                             QuatAngleDeg(c.mount, m_imuBase[hand].mount) <= kAverageWithinDeg;
        if (average) {
            c.mount = Normalize(Slerp(m_imuBase[hand].mount, c.mount, 0.5));
            if (m_imuBase[hand].lag > 0) c.lag = c.lag > 0 ? 0.5 * (c.lag + m_imuBase[hand].lag) : m_imuBase[hand].lag;
        }
        m_imuSaved[hand] = c;
        m_imuHaveSaved[hand] = true;
        m_imuCalDirty = true;
        if (first)
            DriverLog("[%s] IMU calibrated (mount fit %.1f deg, lag %.0f ms): %s\n", HandName(hand), st.residualDeg,
                      st.lag * 1e3, average ? "averaged into the saved calibration" : "saved for the next session");
    }
    if (m_imuCalDirty && now >= m_nextImuCalSave) {
        m_nextImuCalSave = now + 60.0;
        SaveImuCalibration();
    }
}

} // namespace cf
