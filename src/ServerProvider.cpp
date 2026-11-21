/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// ServerProvider.cpp — driver setup and the per-frame loop
// ═══════════════════════════════════════════════════════════════════════════

#include "ServerProvider.h"
#include "TrackingTrust.h"
#include "Utils.h"
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <sstream>
#ifdef _WIN32
#  include <windows.h>
#endif

namespace cf {

namespace {

// "MENU", "STSEL", "C|D", "NONE" … → CyberFingerButton mask.
uint8_t ParseButtons(const std::string& spec) {
    uint8_t mask = 0;
    std::string token;
    std::stringstream ss(spec);
    while (std::getline(ss, token, '|')) {
        std::string t;
        for (char ch : token)
            if (!std::isspace(uint8_t(ch))) t += char(std::toupper(uint8_t(ch)));
        if (t == "TRIGGER" || t == "TRIG") mask |= kBtnTrigger;
        else if (t == "GRIP") mask |= kBtnGrip;
        else if (t == "C") mask |= kBtnC;
        else if (t == "D") mask |= kBtnD;
        else if (t == "E") mask |= kBtnE;
        else if (t == "MENU") mask |= kBtnMenu;
        else if (t == "STICK" || t == "JCLK" || t == "STICKCLICK") mask |= kBtnStickClick;
        else if (t == "STSEL" || t == "STARTSELECT" || t == "START") mask |= kBtnStartSelect;
        else if (!t.empty() && t != "NONE") DriverLog("Settings: unknown button name '%s'\n", t.c_str());
    }
    return mask;
}

vr::EVRSkeletalTrackingLevel ParseTrackingLevel(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return char(std::tolower(c)); });
    if (s == "partial") return vr::VRSkeletalTracking_Partial;
    if (s == "estimated") return vr::VRSkeletalTracking_Estimated;
    return vr::VRSkeletalTracking_Full;
}

// SlimeVR's body trackers by the role in their controller type (slimevr_tracker_<role>): the code a capture
// records them with (kind 8), -1 for anything else. The arm model needs the elbows (and the chest, if worn).
int BodyTrackerCode(const std::string& type) {
    static const char* const kRoles[] = { "left_elbow", "right_elbow", "chest", "waist", "left_knee", "right_knee",
                                          "left_foot", "right_foot", "left_hand", "right_hand", "left_shoulder",
                                          "right_shoulder", "left_upper_arm", "right_upper_arm" };
    static const std::string kPrefix = "slimevr_tracker_";
    if (type.compare(0, kPrefix.size(), kPrefix) != 0) return -1;
    for (int i = 0; i < int(sizeof(kRoles) / sizeof(kRoles[0])); ++i)
        if (type.compare(kPrefix.size(), std::string::npos, kRoles[i]) == 0) return i;
    return -1;
}

void ToFloats(const Xform& x, float pos[3], float rot[4]) {
    pos[0] = float(x.p.x); pos[1] = float(x.p.y); pos[2] = float(x.p.z);
    rot[0] = float(x.q.w); rot[1] = float(x.q.x); rot[2] = float(x.q.y); rot[3] = float(x.q.z);
}

} // namespace

vr::EVRInitError ServerProvider::Init(vr::IVRDriverContext* pDriverContext) {
    VR_INIT_SERVER_DRIVER_CONTEXT(pDriverContext);
    SetDriverLog(vr::VRDriverLog());
    DriverLog("═══ CyberFinger driver 2.0 ═══\n");

    const std::string serialL = SettingString("serialNumber_left", "CYBERFINGER_L");
    const std::string serialR = SettingString("serialNumber_right", "CYBERFINGER_R");

    // ── optical tap: must be hooked before any other driver adds its devices ──
    m_tapEnabled = SettingBool("optical_tap", true);
    m_tap = std::make_unique<OpticalTap>();
    const std::string handTypes =
        SettingString("tap_controller_types", "svl_hand_interaction_augmented|vd_hand_controller");
    m_handSourceTypes = SplitList(handTypes);
    m_tap->Configure(SettingString("tap_serial_left", "Hand_Left"), SettingString("tap_serial_right", "Hand_Right"),
                     handTypes, serialL, serialR, SettingInt("hand_selection_priority", 1000));
    if (m_tapEnabled && SettingBool("optical_tap_hook", true)) {
        if (!m_tap->InstallHooks(pDriverContext))
            DriverLog("Optical tap: skeleton hook unavailable; using headset hand poses only\n");
    }

    // The CyberFinger IMUs, sent by the bridge while a capture runs, go into the capture with everything else.
    // Captures record every packet; the fusion takes the joint IMU, stamped when the report reached the bridge
    // over BLE (the bridge's clock is the driver's; if it ever isn't, the arrival here).
    for (ImuFusion& f : m_imuFusion) f.Reset();
    LoadImuCalibration();
    m_link.SetImuSink([this, tap = m_tap.get()](const ImuPacket& p, double arrival) {
        tap->CaptureImu(p, arrival);
        if (!(p.present & kImuJoint)) return;
        double t = (double(p.h.t_send_us) - double(p.h.age_us)) * 1e-6;
        if (std::fabs(arrival - t) > 0.5) t = arrival;
        m_imuFusion[p.h.hand & 1].AddImu(t, Quat{ p.quat[2][0], p.quat[2][1], p.quat[2][2], p.quat[2][3] });
    });
    m_link.Start(SettingInt("handtracking_udp_port", 27015), SettingInt("context_udp_port", 27016),
                 SettingBool("bind_loopback_only", true), SettingBool("legacy_5bit_buttons", false));

    for (int hand = 0; hand < 2; ++hand) {
        CyberFingerController::Config cfg;
        cfg.serial = hand ? serialR : serialL;
        cfg.trackingLevel = ParseTrackingLevel(SettingString("skeletal_tracking_level", "full"));
        cfg.handPriority = SettingInt("hand_selection_priority", 1000);
        cfg.grabTapTime = std::max(0, SettingInt("grab_tap_ms", 200)) / 1000.0;
        cfg.maskA = ParseButtons(SettingString("button_a", "STSEL"));
        cfg.maskB = ParseButtons(SettingString("button_b", "MENU"));
        cfg.maskSystem = ParseButtons(SettingString("button_system", "NONE"));
        cfg.forwardTapSystem = SettingBool("forward_tap_system_button", false);
        cfg.blackHoldTime = std::max(0, SettingInt("black_hold_ms", 800)) / 1000.0;
        cfg.fusedTimeout = std::max(0.02, SettingInt("fused_timeout_ms", 150) / 1000.0);
        cfg.disconnectAfter = std::max(0, SettingInt("disconnect_after_ms", 0)) / 1000.0;

        // Only used before any headset skeleton has been seen (e.g. hook disabled):
        // the legacy grip_angle_* / pose_offset_* correction, mirrored for the left hand.
        const double ax = SettingFloat("grip_angle_x", -60.f), ay = SettingFloat("grip_angle_y", 35.f),
                     az = SettingFloat("grip_angle_z", 0.f);
        const double ox = SettingFloat("pose_offset_x", 0.f), oy = SettingFloat("pose_offset_y", -0.10f),
                     oz = SettingFloat("pose_offset_z", 0.f);
        const double m = hand ? 1.0 : -1.0;
        cfg.noSkeletonOffset = { QuatFromEulerXYZDeg(ax, ay * m, az * m), { ox * m, oy, oz } };

        m_controller[hand] = std::make_unique<CyberFingerController>(hand, cfg);
        m_controller[hand]->SetImuFusion(&m_imuFusion[hand]);
        vr::VRServerDriverHost()->TrackedDeviceAdded(cfg.serial.c_str(), vr::TrackedDeviceClass_Controller,
                                                     m_controller[hand].get());
    }
    // Republish the headset hands' updates as they arrive (off: once per frame).
    if (m_tapEnabled && SettingBool("passthrough_events", true)) {
        {
            std::lock_guard<std::mutex> g(m_pubLock);
            m_pubRun = true;
        }
        m_pubThread = std::thread(&ServerProvider::RepublishThread, this);
        m_tap->SetListener(this);
    }
    m_lastStatus = NowSeconds();

    DriverLog("CyberFinger driver initialized (optical tap %s, skeleton hook %s, pose hook %s)\n",
              m_tapEnabled ? "on" : "off", m_tap->HooksActive() ? "active" : "off",
              m_tap->PoseHookActive() ? "active" : "off");
    return vr::VRInitError_None;
}

void ServerProvider::Cleanup() {
    DriverLog("CyberFinger driver shutting down\n");
    if (m_tap) {
        m_tap->SetListener(nullptr);
        m_tap->RemoveHooks();   // waits for detours in flight, so no callback outlives the controllers
    }
    {
        std::lock_guard<std::mutex> g(m_pubLock);
        m_pubRun = false;
    }
    m_pubWake.notify_all();
    if (m_pubThread.joinable()) m_pubThread.join();
    RestoreHiddenControllers();
    if (m_imuCalDirty) SaveImuCalibration();
    m_link.Stop();
    m_controller[0].reset();
    m_controller[1].reset();
    m_tap.reset();
    VR_CLEANUP_SERVER_DRIVER_CONTEXT();
}

void ServerProvider::RunFrame() {
    vr::VREvent_t ev;
    while (vr::VRServerDriverHost()->PollNextEvent(&ev, sizeof(ev))) {
        if (ev.eventType == vr::VREvent_Input_HapticVibration) OnHaptic(ev.data.hapticVibration);
    }

    vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
    vr::VRServerDriverHost()->GetRawTrackedDevicePoses(0.f, poses, vr::k_unMaxTrackedDeviceCount);
    if (m_tapEnabled) m_tap->Update(poses, vr::k_unMaxTrackedDeviceCount);

    const double now = NowSeconds();
    if (now >= m_nextSettingsPoll) {
        // Live on/off from SteamVR's settings page; off hands the roles back to the headset's hands.
        m_nextSettingsPoll = now + 0.5;
        const bool active = SettingBool("active", true);
        if (active != m_active) {
            m_active = active;
            DriverLog("CyberFinger controllers %s\n",
                      active ? "active" : "switched off: the headset's own hand tracking takes the hand roles");
        }
        PollPoseFilter();
        PollCaptureRequest();
        m_hideSetting = SettingBool("hide_other_hand_controllers", true);
        m_yieldSetting = SettingBool("yield_to_controllers", true);
        const bool tapToHold = SettingBool("grab_tap_to_hold", true);
        if (tapToHold != m_grabTapToHold || !m_grabTapKnown) {
            m_grabTapToHold = tapToHold;
            m_grabTapKnown = true;
            for (auto& c : m_controller)
                if (c) c->SetGrabTapToHold(tapToHold);
            DriverLog("Grab: tap to hold %s\n", tapToHold ? "on (a quick tap of the grip holds until the next press)"
                                                         : "off (the grab follows the grip button)");
        }
        const bool fuse = SettingBool("imu_fusion", true);
        if (fuse != m_imuFusionEnabled || !m_imuFusionKnown) {
            m_imuFusionEnabled = fuse;
            m_imuFusionKnown = true;
            for (auto& c : m_controller)
                if (c) c->SetImuFusionEnabled(fuse);
            DriverLog("IMU fusion %s\n", fuse ? "on: the CyberFinger's joint IMU drives the hand orientation once calibrated"
                                              : "off: orientation from the headset alone");
        }
    }
    if (m_tapEnabled) m_tap->PollCapture(now);
    if (m_tapEnabled && m_tap->Capturing()) {       // the headset and SlimeVR's body trackers, for the arm model
        m_tap->CaptureDevicePose(7, 0, poses[vr::k_unTrackedDeviceIndex_Hmd], now);
        for (const auto& [index, code] : m_bodyTrackers) m_tap->CaptureDevicePose(8, code, poses[index], now);
    }
    TapHandSnapshot tap[2] = { m_tap->Get(0), m_tap->Get(1) };
    UpdateImuCalibration(tap, now);

    // Other hand controllers: released to when the user picks them up, hidden from apps otherwise. A released
    // hand's controllers are shown before CyberFinger lets go of the hand (same frame, below).
    ScanOtherControllers(now);
    UpdateHandoff(poses, tap, now);
    const bool hide[2] = { m_active && m_hideSetting && !m_yield[0], m_active && m_hideSetting && !m_yield[1] };
    UpdateHiddenControllers(hide, m_active && m_hideSetting);

    // How far the headset's tracking of each hand is trusted: where the hand is seen from, the other hand in front
    // of it (TrackingTrust.h). The other hand counts while its tracking is live, not while a streamer holds it.
    const vr::TrackedDevicePose_t& hmd = poses[vr::k_unTrackedDeviceIndex_Hmd];
    for (int hand = 0; hand < 2; ++hand) {
        if (!m_controller[hand]) continue;
        double trust = 1.0;
        if (hmd.bPoseIsValid && tap[hand].poseValid) {
            const TapHandSnapshot& o = tap[1 - hand];
            const bool otherLive = o.poseValid && o.skeletonAge < 0.3;
            trust = TrackingTrust(hand, XformFromMatrix(hmd.mDeviceToAbsoluteTracking), tap[hand].rawPose.p,
                                  otherLive ? &o.rawPose.p : nullptr);
        }
        m_controller[hand]->SetTrackingTrust(trust);
        const Xform head = XformFromMatrix(hmd.mDeviceToAbsoluteTracking);
        m_controller[hand]->SetHeadPosition(hmd.bPoseIsValid != 0, head.p);
    }

    // The bridge asks for an IMU fusion resync (its button, a triple tap on the CyberFinger) with a new count in CFG2: a
    // lost packet only delays it. The first count seen from a bridge is its start, not a request.
    for (int hand = 0; hand < 2; ++hand) {
        const CyberFingerState g = m_link.CyberFinger(hand);
        if (!g.valid) continue;
        if (m_resyncSeen[hand] >= 0 && g.resync != m_resyncSeen[hand]) {
            m_imuFusion[hand].Resync();
            m_imuResyncsLogged[hand] = m_imuFusion[hand].GetStatus().resyncs;
            DriverLog("[%s] IMU fusion resync, asked for by the bridge\n", hand ? "right" : "left");
        }
        m_resyncSeen[hand] = g.resync;
    }

    // The headset's hand gesture for the system button (the Quest palm pinch) opens nothing while CyberFinger is
    // active; forward_tap_system_button passes it on as CyberFinger's own system button instead.
    m_tap->SetBlockHandSystem(m_active);
    for (int hand = 0; hand < 2; ++hand)
        if (m_controller[hand]) {
            m_tap->SetOwnDevice(hand, m_controller[hand]->ObjectId());
            m_controller[hand]->SetSourceFiltered(FilteredSource(tap[hand].controllerType), tap[hand].controllerType);
            m_controller[hand]->Update(m_link.CyberFinger(hand), m_link.HandState(hand), tap[hand], now,
                                       m_active && !m_yield[hand]);
        }

    if (now >= m_nextContext) {
        m_nextContext = now + 1.0 / 120.0;
        SendContext(poses, tap, now);
    }
    if (now >= m_nextStatus) {
        m_nextStatus = now + 10.0;
        LogStatus(tap, now);
    }
}

namespace {
// Set while RepublishThread submits: updates that other drivers' hooks send back to us in response are
// echoes of our own submissions, not new data.
thread_local bool t_republishing = false;
} // namespace

void ServerProvider::OnTapPose(int hand, const vr::DriverPose_t& pose) {
    if (t_republishing) return;
    {
        std::lock_guard<std::mutex> g(m_pubLock);
        Pending& p = m_pending[hand & 1];
        p.pose = pose;
        p.poseArrival = NowSeconds();
        p.hasPose = true;
    }
    m_pubWake.notify_one();
}

void ServerProvider::OnTapSkeleton(int hand, vr::EVRSkeletalMotionRange range, const vr::VRBoneTransform_t* bones,
                                   uint32_t count) {
    // Only WithoutController is republished (both of our ranges follow it).
    if (t_republishing || range != vr::VRSkeletalMotionRange_WithoutController || !bones ||
        count < uint32_t(eBone_Count))
        return;
    {
        std::lock_guard<std::mutex> g(m_pubLock);
        Pending& p = m_pending[hand & 1];
        std::copy(bones, bones + eBone_Count, p.bones);
        p.hasSkeleton = true;
    }
    m_pubWake.notify_one();
}

void ServerProvider::RepublishThread() {
    t_republishing = true;
    std::unique_lock<std::mutex> lock(m_pubLock);
    for (;;) {
        m_pubWake.wait(lock, [this] {
            return !m_pubRun || m_pending[0].hasPose || m_pending[0].hasSkeleton || m_pending[1].hasPose ||
                   m_pending[1].hasSkeleton;
        });
        if (!m_pubRun) return;
        Pending work[2];
        for (int hand = 0; hand < 2; ++hand) {
            work[hand] = m_pending[hand];
            m_pending[hand].hasPose = m_pending[hand].hasSkeleton = false;
        }
        lock.unlock();
        for (int hand = 0; hand < 2; ++hand) {
            CyberFingerController* c = m_controller[hand].get();
            if (!c) continue;
            // Skeleton first: it carries the wrist the pose is re-rooted with.
            if (work[hand].hasSkeleton)
                c->OnTapSkeleton(vr::VRSkeletalMotionRange_WithoutController, work[hand].bones, eBone_Count);
            if (work[hand].hasPose) {
                vr::DriverPose_t pose = work[hand].pose;
                pose.poseTimeOffset -= NowSeconds() - work[hand].poseArrival;   // the same instant, submitted later
                c->OnTapPose(pose);
            }
        }
        lock.lock();
    }
}

// The PASSTHROUGH pose filter's settings, read live so it can be compared on and off in the headset.
void ServerProvider::PollPoseFilter() {
    PoseFilter::Params p;
    p.enabled = SettingBool("pose_filter", true);
    p.minCutoff = std::max(0.05f, SettingFloat("pose_filter_min_cutoff", 1.0f));
    p.beta = std::max(0.f, SettingFloat("pose_filter_beta", 15.f));
    p.rotMinCutoff = p.minCutoff;
    p.prediction = std::clamp(SettingFloat("pose_prediction", 0.5f), 0.f, 2.f);
    p.rotPrediction = std::clamp(SettingFloat("pose_rotation_prediction", 0.f), 0.f, 2.f);
    p.gate = std::max(0.005f, SettingFloat("pose_filter_gate_cm", 5.f) / 100.f);
    // Only these sources need it: Steam Link streams predicted, noisy poses; Virtual Desktop's are clean.
    const std::string types = SettingString("pose_filter_types", "svl_hand_interaction_augmented");
    const bool changed = !m_filterKnown || p.enabled != m_filter.enabled || p.minCutoff != m_filter.minCutoff ||
                         p.beta != m_filter.beta || p.prediction != m_filter.prediction ||
                         p.rotPrediction != m_filter.rotPrediction || p.gate != m_filter.gate ||
                         types != m_filterTypesSetting;
    if (!changed) return;
    m_filter = p;
    m_filterKnown = true;
    m_filterTypesSetting = types;
    m_filterTypes = SplitList(types);
    for (auto& c : m_controller)
        if (c) c->SetPoseFilter(p);
    DriverLog("Pose filter %s for %s (min cutoff %.2f Hz, beta %.1f, prediction %.2f, rotation %.2f, gate %.1f cm)\n",
              p.enabled ? "on" : "off", types.c_str(), p.minCutoff, p.beta, p.prediction, p.rotPrediction,
              p.gate * 100);
}

bool ServerProvider::FilteredSource(const std::string& controllerType) const {
    const std::string t = Lower(controllerType);
    return std::find(m_filterTypes.begin(), m_filterTypes.end(), t) != m_filterTypes.end();
}

// Other drivers' hand controllers: the Touch controllers Steam Link and Virtual Desktop emulate from hand
// tracking, or the real ones when the user picks them up. Every second, and every frame for a second after a
// device is added: Steam Link adds its controllers when they're first picked up, and apps look at a device as
// soon as it's activated (Resonite makes a role-less controller a tracker for good), so it must be hidden at
// once; its properties arrive during its activation, hence the repeated scans. Frame loop.
void ServerProvider::ScanOtherControllers(double now) {
    vr::CVRPropertyHelpers* props = vr::VRProperties();
    uint32_t devices = 0;
    for (uint32_t i = 1; i < vr::k_unMaxTrackedDeviceCount; ++i)
        if (props->TrackedDeviceToPropertyContainer(i) != vr::k_ulInvalidPropertyContainer) ++devices;
    if (devices != m_deviceCount) {
        m_deviceCount = devices;
        m_fastScanUntil = now + 1.0;
    }
    if (now < m_nextScan && now >= m_fastScanUntil) return;
    m_nextScan = now + 1.0;
    vr::PropertyContainerHandle_t ours[2] = { vr::k_ulInvalidPropertyContainer, vr::k_ulInvalidPropertyContainer };
    for (int h = 0; h < 2; ++h)
        if (m_controller[h] && m_controller[h]->ObjectId() != vr::k_unTrackedDeviceIndexInvalid)
            ours[h] = props->TrackedDeviceToPropertyContainer(m_controller[h]->ObjectId());
    m_others.clear();
    m_handSources.clear();
    m_bodyTrackers.clear();
    for (uint32_t i = 1; i < vr::k_unMaxTrackedDeviceCount; ++i) {           // 0 is the headset
        const vr::PropertyContainerHandle_t c = props->TrackedDeviceToPropertyContainer(i);
        if (c == vr::k_ulInvalidPropertyContainer || c == ours[0] || c == ours[1]) continue;
        const std::string type = Lower(props->GetStringProperty(c, vr::Prop_ControllerType_String));
        if (const int body = BodyTrackerCode(type); body >= 0) {
            m_bodyTrackers.push_back({ i, uint8_t(body) });
            continue;
        }
        if (std::find(m_handSourceTypes.begin(), m_handSourceTypes.end(), type) != m_handSourceTypes.end()) {
            m_handSources.push_back(c);
            continue;
        }
        vr::ETrackedPropertyError err = vr::TrackedProp_Success;
        if (props->GetInt32Property(c, vr::Prop_DeviceClass_Int32, &err) != vr::TrackedDeviceClass_Controller ||
            err != vr::TrackedProp_Success)
            continue;
        const int32_t role = props->GetInt32Property(c, vr::Prop_ControllerRoleHint_Int32, &err);
        if (err != vr::TrackedProp_Success ||
            (role != vr::TrackedControllerRole_LeftHand && role != vr::TrackedControllerRole_RightHand))
            continue;
        if (type == "cyberfinger") continue;                                    // ours
        m_others.push_back({ c, i, role == vr::TrackedControllerRole_LeftHand ? 0 : 1 });
    }
}

// The Quest tracks either the hands or the controllers. When a hand's tracking has stopped while a controller
// for that hand is tracked, the user picked the controllers up: release the hand to the controller. Take it back
// as soon as hand tracking is live again (the controllers can't tell: Virtual Desktop's emulated ones track the
// hands then). Frame loop.
//
// The skeleton is the signal: both streamers stop it at once when the user switches to the controllers, but keep
// the lost hand's pose valid (frozen) for seconds. It can't be mistaken for a hand out of view: Virtual Desktop
// then disconnects that hand's emulated Touch controller at the same moment (so no controller is tracked), and
// Steam Link keeps sending the frozen skeleton (so it doesn't stop); Steam Link's controllers only exist and track
// while real controllers are in use. Hand over after 0.3 s without a skeleton, confirmed for 0.2 s.
void ServerProvider::UpdateHandoff(const vr::TrackedDevicePose_t* poses, const TapHandSnapshot tap[2], double now) {
    for (int h = 0; h < 2; ++h) {
        if (!m_controller[h]) continue;
        const char* serial = m_controller[h]->Serial().c_str();
        if (!m_yieldSetting || !m_active) {
            if (m_yield[h]) {
                DriverLog("[%s] taking the hand back (yield_to_controllers or active changed); IMU fusion starts over\n",
                          serial);
                ColdStartImu(h);
            }
            m_yield[h] = false;
            m_yieldSince[h] = m_returnSince[h] = -1;
            continue;
        }
        if (!m_yield[h]) {
            bool controllerTracked = false;
            for (const OtherController& o : m_others) {
                const vr::TrackedDevicePose_t& p = poses[o.index];
                if (o.hand == h && p.bDeviceIsConnected && p.bPoseIsValid &&
                    p.eTrackingResult == vr::TrackingResult_Running_OK)
                    controllerTracked = true;
            }
            const bool handLost = tap[h].skeletonAge > 0.3;
            if (!(handLost && controllerTracked)) m_yieldSince[h] = -1;
            else if (m_yieldSince[h] < 0) m_yieldSince[h] = now;
            if (m_yieldSince[h] >= 0 && now - m_yieldSince[h] >= 0.2) {
                m_yield[h] = true;
                m_returnSince[h] = -1;
                DriverLog("[%s] hand tracking stopped and a controller is tracked: releasing the hand to it\n",
                          serial);
            }
        } else {
            const bool handLive = tap[h].poseValid && tap[h].skeletonAge < 0.25;
            if (!handLive) m_returnSince[h] = -1;
            else if (m_returnSince[h] < 0) m_returnSince[h] = now;
            if (m_returnSince[h] >= 0 && now - m_returnSince[h] >= 0.3) {
                m_yield[h] = false;
                m_yieldSince[h] = -1;
                DriverLog("[%s] hand tracking is back: taking the hand again; IMU fusion starts over, as from cold\n",
                          serial);
                ColdStartImu(h);
            }
        }
    }
}

// While CyberFinger holds a hand, the other controllers for it are left without a role, and apps show them
// anyway: Resonite maps role-less controllers as trackers and draws them on the hands, or registers them as its
// hands instead. Marked Prop_NeverTracked, they are skipped (Resonite checks it when a device connects). Shown
// again when CyberFinger lets go of the hand. Frame loop, after ScanOtherControllers.
//
// The headset's hand-tracking devices (hideHandSources: while CyberFinger is active) the same, whether CyberFinger
// holds the hand or not: it takes the hand back 0.3 s after they come back, and in between SteamVR can give them the
// role and Resonite register them. With a binding that simulates Touch, SteamVR reports the same serial for them as
// for CyberFinger ("<headset>_Controller_Left"), and Resonite's engine drives one controller from both: the idle one
// overwrites CyberFinger's pose and input every frame. CyberFinger still reads them through its hooks (OpticalTap).
void ServerProvider::UpdateHiddenControllers(const bool hide[2], bool hideHandSources) {
    vr::CVRPropertyHelpers* props = vr::VRProperties();
    std::map<vr::PropertyContainerHandle_t, const char*> want;   // container -> while what
    for (const OtherController& o : m_others)
        if (hide[o.hand]) want[o.container] = "while CyberFinger holds its hand";
    if (hideHandSources)
        for (const vr::PropertyContainerHandle_t c : m_handSources) want[c] = "while CyberFinger is active";
    for (const auto& [c, why] : want) {
        if (m_hidden.count(c) || m_hideRefused.count(c)) continue;
        const vr::ETrackedPropertyError err = props->SetBoolProperty(c, vr::Prop_NeverTracked_Bool, true);
        const std::string serial = props->GetStringProperty(c, vr::Prop_SerialNumber_String);
        const std::string type = props->GetStringProperty(c, vr::Prop_ControllerType_String);
        if (err == vr::TrackedProp_Success) {
            m_hidden.insert(c);
            DriverLog("Hiding %s (%s) from apps %s (never tracked)\n", serial.c_str(), type.c_str(), why);
        } else {
            m_hideRefused.insert(c);
            DriverLog("Could not hide %s (%s): property error %d\n", serial.c_str(), type.c_str(), int(err));
        }
    }
    for (auto it = m_hidden.begin(); it != m_hidden.end();) {
        if (want.count(*it)) { ++it; continue; }
        props->EraseProperty(*it, vr::Prop_NeverTracked_Bool);
        DriverLog("Showing %s to apps again\n", props->GetStringProperty(*it, vr::Prop_SerialNumber_String).c_str());
        it = m_hidden.erase(it);
    }
}

void ServerProvider::RestoreHiddenControllers() {
    for (const vr::PropertyContainerHandle_t c : m_hidden) vr::VRProperties()->EraseProperty(c, vr::Prop_NeverTracked_Bool);
    m_hidden.clear();
}

// %LOCALAPPDATA%\CyberFinger\capture_request.txt (content: seconds) starts a capture of the headset hands'
// raw updates into %LOCALAPPDATA%\CyberFinger\captures\ (tools/analyze_tap_capture.py writes the request).
// A debugging tool: only with the setting debug_captures on.
void ServerProvider::PollCaptureRequest() {
    const char* base = std::getenv("LOCALAPPDATA");
    if (!m_tapEnabled || !base) return;
    const std::string dir = std::string(base) + "\\CyberFinger";
    const std::string request = dir + "\\capture_request.txt";
    std::FILE* f = std::fopen(request.c_str(), "r");
    if (!f) return;
    double seconds = 10;
    if (std::fscanf(f, "%lf", &seconds) != 1 || seconds <= 0 || seconds > 120) seconds = 10;
    std::fclose(f);
    std::remove(request.c_str());
    if (!SettingBool("debug_captures", false)) {
        DriverLog("Capture request ignored: turn on 'Debug captures' (debug_captures) in the CyberFinger settings\n");
        return;
    }
    const std::time_t now = std::time(nullptr);
    char stamp[32];
    std::strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", std::localtime(&now));
    std::string path = dir + "\\captures";
    CreateDirectoryA(path.c_str(), nullptr);
    path += std::string("\\tap_") + stamp + ".csv";
    m_tap->StartCapture(seconds, path);
}

// ── IMU calibration across sessions ──
// The joint IMU's mounting belongs to the CyberFinger and how it sits, the lag to the streamer: both carry over, so
// the fusion starts from them and only fits the IMU's heading (new with every power-up) — within a second of
// seeing the hand, instead of waiting for enough varied orientations for a full solve. Before anything is saved
// it starts from the reference CyberFingers' calibration. The file is plain text; deleting it starts afresh.
namespace {

std::string ImuCalibrationPath() {
    const char* base = std::getenv("LOCALAPPDATA");
    return base ? std::string(base) + "\\CyberFinger\\imu_calibration.txt" : std::string();
}

double QuatAngleDeg(const Quat& a, const Quat& b) {
    const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
    return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
}

constexpr double kSaveMaxResidualDeg = 15.0;   // only a good full solve is saved (sessions fit to 4-8°)
constexpr double kAverageWithinDeg = 25.0;      // a solve this close to the saved one is averaged with it

} // namespace

// Lines: <left|right> <source controller type> <mount w x y z> <lag ms> <fit deg>; '#' comments.
void ServerProvider::LoadImuCalibration() {
    const std::string path = ImuCalibrationPath();
    std::FILE* f = path.empty() ? nullptr : std::fopen(path.c_str(), "r");
    if (!f) return;
    char line[512];
    int n = 0;
    while (std::fgets(line, sizeof(line), f)) {
        if (line[0] == '#') continue;
        std::istringstream ss(line);
        std::string hand, source;
        ImuFusion::Calibration c;
        double lagMs = 0;
        if (!(ss >> hand >> source >> c.mount.w >> c.mount.x >> c.mount.y >> c.mount.z >> lagMs >> c.residualDeg))
            continue;
        const double norm = std::sqrt(c.mount.w * c.mount.w + c.mount.x * c.mount.x + c.mount.y * c.mount.y +
                                      c.mount.z * c.mount.z);
        if ((hand != "left" && hand != "right") || norm < 0.5 || norm > 1.5) continue;
        c.mount = Normalize(c.mount);
        c.lag = std::max(0.0, lagMs * 1e-3);
        m_imuSaved[hand == "right"][Lower(source)] = c;
        ++n;
    }
    std::fclose(f);
    DriverLog("IMU calibration: %d saved (%s)\n", n, path.c_str());
}

void ServerProvider::SaveImuCalibration() {
    const std::string path = ImuCalibrationPath();
    if (path.empty()) return;
#ifdef _WIN32
    CreateDirectoryA(path.substr(0, path.rfind('\\')).c_str(), nullptr);
#endif
    const std::string tmp = path + ".tmp";
    std::FILE* f = std::fopen(tmp.c_str(), "w");
    if (!f) {
        DriverLog("IMU calibration: cannot write %s\n", tmp.c_str());
        return;
    }
    std::fprintf(f, "# CyberFinger IMU calibration, kept by the driver: how the joint IMU sits on each hand, and\n"
                    "# the hand-tracking source's lag, per hand and source. The IMU fusion starts from it; delete this\n"
                    "# file to start afresh.\n"
                    "# hand source mount_w mount_x mount_y mount_z lag_ms fit_deg\n");
    for (int hand = 0; hand < 2; ++hand)
        for (const auto& [source, c] : m_imuSaved[hand])
            std::fprintf(f, "%s %s %.5f %.5f %.5f %.5f %.1f %.1f\n", hand ? "right" : "left", source.c_str(),
                         c.mount.w, c.mount.x, c.mount.y, c.mount.z, c.lag * 1e3, c.residualDeg);
    const bool ok = std::fclose(f) == 0;
#ifdef _WIN32
    if (ok && MoveFileExA(tmp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING)) m_imuCalDirty = false;
#else
    if (ok && std::rename(tmp.c_str(), path.c_str()) == 0) m_imuCalDirty = false;
#endif
    if (m_imuCalDirty) DriverLog("IMU calibration: cannot replace %s\n", path.c_str());
}

// Start a hand's IMU fusion over as at a cold start: from the calibration saved for its hand-tracking source (this
// session's good solves are averaged into it), the lag measured again. For a hand taken back from the controllers:
// the headset lost it for the whole time, the IMU's heading drifted, and handling the controllers may have moved the
// CyberFinger on the hand. A resync would start from this session's last solve instead, which fits worse when the CyberFinger
// moved (2026-09-27: 18° against the saved calibration's ~11°, and two minutes to solve again, against 23 s from cold).
// Frame loop.
void ServerProvider::ColdStartImu(int hand) {
    if (m_imuSource[hand].empty()) return;
    const auto it = m_imuSaved[hand].find(m_imuSource[hand]);
    m_imuFusion[hand].Reset();
    m_imuFusion[hand].SetPrior(it != m_imuSaved[hand].end() ? it->second : m_imuBase[hand]);
    m_imuSavedSolves[hand] = 0;   // log its next calibration like the first
}

void ServerProvider::UpdateImuCalibration(const TapHandSnapshot tap[2], double now) {
    if (now < m_nextImuCalCheck) return;
    m_nextImuCalCheck = now + 0.5;
    for (int hand = 0; hand < 2; ++hand) {
        const char* name = hand ? "right" : "left";
        // A hand-tracking source appeared (or another replaced it): start from its calibration.
        const std::string source = Lower(tap[hand].controllerType);
        if (!source.empty() && source != m_imuSource[hand]) {
            if (!m_imuSource[hand].empty()) m_imuFusion[hand].Reset();
            m_imuSource[hand] = source;
            m_imuSavedSolves[hand] = 0;
            const auto it = m_imuSaved[hand].find(source);
            m_imuHaveBase[hand] = it != m_imuSaved[hand].end();
            m_imuBase[hand] = m_imuHaveBase[hand] ? it->second : ImuFusion::DefaultCalibration(hand);
            m_imuFusion[hand].SetPrior(m_imuBase[hand]);
            DriverLog("[%s] IMU fusion starts from %s (lag %.0f ms)\n", name,
                      m_imuHaveBase[hand] ? "the calibration saved for this source" : "the reference CyberFinger's calibration",
                      m_imuBase[hand].lag * 1e3);
        }
        // A new good full solve: remember it. Sessions scatter by ~15° (how the CyberFinger sits, the postures seen),
        // so a solve near the saved calibration is averaged with it rather than replacing it.
        const ImuFusion::Status st = m_imuFusion[hand].GetStatus();
        if (st.resyncs < m_imuResyncsLogged[hand]) m_imuResyncsLogged[hand] = st.resyncs;       // after a Reset
        if (st.resyncs > m_imuResyncsLogged[hand]) {
            m_imuResyncsLogged[hand] = st.resyncs;
            DriverLog("[%s] IMU fusion resync: the joint IMU came back after a gap (the CyberFinger switched off?)\n", name);
        }
        ImuFusion::Calibration c;
        if (m_imuSource[hand].empty() || st.solves == m_imuSavedSolves[hand] || st.residualDeg > kSaveMaxResidualDeg ||
            !m_imuFusion[hand].GetCalibration(c))
            continue;
        const bool first = m_imuSavedSolves[hand] == 0;
        m_imuSavedSolves[hand] = st.solves;
        const double moved = QuatAngleDeg(c.mount, m_imuBase[hand].mount);
        const bool average = m_imuHaveBase[hand] && moved <= kAverageWithinDeg;
        if (average) {
            c.mount = Normalize(Slerp(m_imuBase[hand].mount, c.mount, 0.5));
            if (m_imuBase[hand].lag > 0) c.lag = c.lag > 0 ? 0.5 * (c.lag + m_imuBase[hand].lag) : m_imuBase[hand].lag;
        }
        m_imuSaved[hand][m_imuSource[hand]] = c;
        m_imuCalDirty = true;
        if (first)
            DriverLog("[%s] IMU calibrated (mount fit %.1f deg, %.1f deg from where it started, lag %.0f ms): %s\n",
                      name, st.residualDeg, moved, st.lag * 1e3,
                      average ? "averaged into the saved calibration" : "saved for the next session");
    }
    if (m_imuCalDirty && now >= m_nextImuCalSave) {
        m_nextImuCalSave = now + 60.0;
        SaveImuCalibration();
    }
}

// An application asked one of our hands to vibrate: pass it to the bridge, which drives the CyberFinger.
void ServerProvider::OnHaptic(const vr::VREvent_HapticVibration_t& hv) {
    for (int hand = 0; hand < 2; ++hand) {
        if (!m_controller[hand] || m_controller[hand]->HapticHandle() != hv.componentHandle) continue;
        m_link.SendHaptic(hand, hv.fDurationSeconds, hv.fFrequency, hv.fAmplitude);
        if (!m_loggedHaptic[hand]) {
            m_loggedHaptic[hand] = true;
            DriverLog("[%s] first haptic event: %.3f s, %.0f Hz, amplitude %.2f\n", m_controller[hand]->Serial().c_str(),
                      hv.fDurationSeconds, hv.fFrequency, hv.fAmplitude);
        }
    }
}

void ServerProvider::SendContext(const vr::TrackedDevicePose_t* poses, const TapHandSnapshot tap[2], double now) {
    ContextPacket pkt{};
    pkt.h.magic = kMagicContext;
    pkt.h.version = kVersion;
    pkt.h.hand = 0xFF;
    pkt.h.flags = (m_tapEnabled && m_tap->Capturing()) ? kCtxCapturing : 0;   // the bridge then sends CFIM
    pkt.h.seq = ++m_contextSeq;
    pkt.h.t_send_us = NowMicros();

    const vr::TrackedDevicePose_t& hmd = poses[vr::k_unTrackedDeviceIndex_Hmd];
    pkt.hmd_valid = hmd.bPoseIsValid && hmd.bDeviceIsConnected;
    ToFloats(XformFromMatrix(hmd.mDeviceToAbsoluteTracking), pkt.hmd_pos, pkt.hmd_rot);
    for (int i = 0; i < 3; ++i) {
        pkt.hmd_lin_vel[i] = hmd.vVelocity.v[i];
        pkt.hmd_ang_vel[i] = hmd.vAngularVelocity.v[i];
    }
    pkt.mode_left = m_controller[0] ? m_controller[0]->Mode() : 0;
    pkt.mode_right = m_controller[1] ? m_controller[1]->Mode() : 0;
    pkt.tap_hook_ok = m_tap->HooksActive() ? 1 : 0;

    for (int hand = 0; hand < 2; ++hand) {
        pkt.applied_hs_seq[hand] = m_controller[hand] ? m_controller[hand]->AppliedSeq() : 0;
        const TapHandSnapshot& s = tap[hand];
        TapHand& t = pkt.tap[hand];
        t.pose_valid = s.poseValid;
        t.skel_valid = s.skeletonValid;
        t.tracking_result = s.trackingResult;
        t.bone_count = uint8_t(std::min<uint32_t>(s.boneCount, 255));
        t.system_click = s.systemClick;
        t.source_found = s.sourceFound;
        t.skel_age_us = uint32_t(std::min(s.skeletonAge, 4000.0) * 1e6);
        ToFloats(s.rawPose, t.raw_pos, t.raw_rot);
        t.lin_vel[0] = float(s.linVel.x); t.lin_vel[1] = float(s.linVel.y); t.lin_vel[2] = float(s.linVel.z);
        t.ang_vel[0] = float(s.angVel.x); t.ang_vel[1] = float(s.angVel.y); t.ang_vel[2] = float(s.angVel.z);
        std::memcpy(t.bones, s.bones, sizeof(t.bones));
    }
    (void)now;
    m_link.SendContext(pkt);
}

void ServerProvider::LogStatus(const TapHandSnapshot tap[2], double now) {
    // Update rates: the headset hand's (pose/skeleton per second) and what we republished from them.
    const double dt = now - m_lastStatus;
    m_lastStatus = now;
    for (int hand = 0; hand < 2; ++hand) {
        const OpticalTap::Rates src = m_tap->TakeRates(dt, hand);
        const auto& c = m_controller[hand];
        if (!c) continue;
        double ourPose = 0, ourSkel = 0;
        c->TakeEventRates(dt, ourPose, ourSkel);
        const OpticalTap::Noise noise = m_tap->TakeNoise(hand);
        char still[96] = "held still: -";
        if (noise.windows > 0)
            std::snprintf(still, sizeof(still), "held still %d s: wrist %.1f, tips %.1f, fingers %.1f mm",
                          noise.windows, noise.wrist * 1e3, noise.tips * 1e3, noise.fingers * 1e3);
        const CyberFingerState g = m_link.CyberFinger(hand);
        const HandStateSample f = m_link.HandState(hand);
        const ImuFusion::Status is = m_imuFusion[hand].GetStatus();
        char imu[128] = "off";
        if (m_imuFusionEnabled && is.calibrated)
            std::snprintf(imu, sizeof(imu), "lag %.0f ms, %s %.1f deg, %zu solves, %zu optical samples refused%s",
                          is.lag * 1e3, is.fromPrior ? "starting mount fits" : "mount fit", is.residualDeg, is.solves,
                          is.rejected, c->ImuFused() ? "" : ", IMU silent");
        else if (m_imuFusionEnabled && is.priorResidualDeg >= 0)
            std::snprintf(imu, sizeof(imu), "calibrating (%zu pairs; the starting mount is off by %.0f deg)", is.pairs,
                          is.priorResidualDeg);
        else if (m_imuFusionEnabled)
            std::snprintf(imu, sizeof(imu), "calibrating (%zu pairs)", is.pairs);
        DriverLog("[%s] status: mode=%u%s tap(src=%s pose=%d skel=%d bones=%u age=%.2fs sys=%d) "
                  "rates(src %.0f/%.0f Hz, new data %.0f/%.0f Hz, republished %.0f/%.0f Hz) noise(%s) CyberFinger=%s fused=%s "
                  "imu(%s)\n",
                  c->Serial().c_str(), unsigned(c->Mode()), c->Following() ? " (event)" : "",
                  tap[hand].serial.empty() ? "-" : tap[hand].serial.c_str(), int(tap[hand].poseValid),
                  int(tap[hand].skeletonValid), tap[hand].boneCount, std::min(tap[hand].skeletonAge, 999.0),
                  int(tap[hand].systemClick), src.pose, src.skeleton, src.poseNew, src.skeletonNew, ourPose, ourSkel, still,
                  g.valid ? (now - g.time < 0.5 ? "live" : "stale") : "none",
                  f.valid ? (now - f.arrival < 0.5 ? "live" : "stale") : "none", imu);
    }
}

} // namespace cf
