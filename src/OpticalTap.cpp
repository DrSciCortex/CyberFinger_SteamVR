/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// OpticalTap.cpp — observation hooks on IVRDriverInput and IVRServerDriverHost
//                  + per-hand snapshots
//
// The hook pattern (MinHook on the implementation functions behind the
// interface vtable) follows OpenVR-SpaceCalibrator. vrserver implements every
// IVRDriverInput version itself, and slots 0–6 are identical in _003, _004 and
// _005, so hooking those functions observes every driver's calls, whichever
// interface version it was built against. SteamVR of 2026-10-21 added _005 and
// Steam Link moved to it; its older versions are adapters that forward into
// _005 (the _004 one jumps to the same slot unchanged for slots 0–9), so only
// the outermost detour on a thread reports a call. TrackedDevicePoseUpdated is
// slot 1 of IVRServerDriverHost _005 and _006 (SpaceCalibrator hooks the same
// slot).
// ═══════════════════════════════════════════════════════════════════════════

#include "OpticalTap.h"
#include "SkeletonSynth.h"
#include "Utils.h"
#ifdef _WIN32
#  include <MinHook.h>
#endif
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

#ifdef _WIN32
static_assert(sizeof(void*) == 8, "the vtable hooks assume the x64 calling convention");
#endif

namespace cf {

namespace {

std::atomic<OpticalTap*> g_tap{ nullptr };

#ifdef _WIN32
using CreateBoolFn = vr::EVRInputError (*)(vr::IVRDriverInput*, vr::PropertyContainerHandle_t, const char*,
                                           vr::VRInputComponentHandle_t*);
using UpdateBoolFn = vr::EVRInputError (*)(vr::IVRDriverInput*, vr::VRInputComponentHandle_t, bool, double);
using CreateScalarFn = vr::EVRInputError (*)(vr::IVRDriverInput*, vr::PropertyContainerHandle_t, const char*,
                                             vr::VRInputComponentHandle_t*, vr::EVRScalarType, vr::EVRScalarUnits);
using UpdateScalarFn = vr::EVRInputError (*)(vr::IVRDriverInput*, vr::VRInputComponentHandle_t, float, double);
using CreateSkelFn = vr::EVRInputError (*)(vr::IVRDriverInput*, vr::PropertyContainerHandle_t, const char*,
                                           const char*, const char*, vr::EVRSkeletalTrackingLevel,
                                           const vr::VRBoneTransform_t*, uint32_t, vr::VRInputComponentHandle_t*);
using UpdateSkelFn = vr::EVRInputError (*)(vr::IVRDriverInput*, vr::VRInputComponentHandle_t,
                                           vr::EVRSkeletalMotionRange, const vr::VRBoneTransform_t*, uint32_t);

constexpr int kSlotCreateBool = 0;
constexpr int kSlotUpdateBool = 1;
constexpr int kSlotCreateScalar = 2;
constexpr int kSlotUpdateScalar = 3;
constexpr int kSlotCreateSkel = 5;
constexpr int kSlotUpdateSkel = 6;

// The older IVRDriverInput versions forward into the latest one, so a call
// through a hooked adapter reaches a second hooked function on the same thread.
// Any IVRDriverInput call made from inside a tap callback counts as nested too
// and goes unseen: the callbacks must not submit (ServerProvider republishes
// from its own thread).
thread_local int t_nested = 0;
struct Outer {
    const bool first = (t_nested++ == 0);
    ~Outer() { --t_nested; }
};

struct Originals {
    CreateBoolFn createBool = nullptr;
    UpdateBoolFn updateBool = nullptr;
    CreateScalarFn createScalar = nullptr;
    UpdateScalarFn updateScalar = nullptr;
    CreateSkelFn createSkel = nullptr;
    UpdateSkelFn updateSkel = nullptr;
};

// One set of detours per interface version, in case vrserver backs the
// versions with different functions (each needs its own trampoline).
template <int N>
struct HookSet {
    static Originals orig;

    static vr::EVRInputError CreateBool(vr::IVRDriverInput* self, vr::PropertyContainerHandle_t c,
                                        const char* name, vr::VRInputComponentHandle_t* h) {
        const Outer o;
        const vr::EVRInputError e = orig.createBool(self, c, name, h);
        if (o.first && e == vr::VRInputError_None && h && name)
            if (OpticalTap* t = g_tap.load(std::memory_order_acquire)) t->OnCreateInput(c, name, *h);
        return e;
    }
    static vr::EVRInputError UpdateBool(vr::IVRDriverInput* self, vr::VRInputComponentHandle_t h,
                                        bool value, double timeOffset) {
        const Outer o;
        bool hold = false;
        if (o.first)
            if (OpticalTap* t = g_tap.load(std::memory_order_acquire)) hold = t->OnUpdateInput(h, value ? 1.f : 0.f);
        return orig.updateBool(self, h, value && !hold, timeOffset);
    }
    static vr::EVRInputError CreateScalar(vr::IVRDriverInput* self, vr::PropertyContainerHandle_t c,
                                          const char* name, vr::VRInputComponentHandle_t* h,
                                          vr::EVRScalarType type, vr::EVRScalarUnits units) {
        const Outer o;
        const vr::EVRInputError e = orig.createScalar(self, c, name, h, type, units);
        if (o.first && e == vr::VRInputError_None && h && name)
            if (OpticalTap* t = g_tap.load(std::memory_order_acquire)) t->OnCreateInput(c, name, *h);
        return e;
    }
    static vr::EVRInputError UpdateScalar(vr::IVRDriverInput* self, vr::VRInputComponentHandle_t h,
                                          float value, double timeOffset) {
        const Outer o;
        if (o.first)
            if (OpticalTap* t = g_tap.load(std::memory_order_acquire)) t->OnUpdateInput(h, value);
        return orig.updateScalar(self, h, value, timeOffset);
    }
    static vr::EVRInputError CreateSkel(vr::IVRDriverInput* self, vr::PropertyContainerHandle_t c,
                                        const char* name, const char* skeletonPath, const char* basePosePath,
                                        vr::EVRSkeletalTrackingLevel level, const vr::VRBoneTransform_t* grip,
                                        uint32_t gripCount, vr::VRInputComponentHandle_t* h) {
        const Outer o;
        const vr::EVRInputError e = orig.createSkel(self, c, name, skeletonPath, basePosePath, level, grip, gripCount, h);
        if (o.first && e == vr::VRInputError_None && h)
            if (OpticalTap* t = g_tap.load(std::memory_order_acquire))
                t->OnCreateSkeleton(c, name, skeletonPath, basePosePath, level, *h);
        return e;
    }
    static vr::EVRInputError UpdateSkel(vr::IVRDriverInput* self, vr::VRInputComponentHandle_t h,
                                        vr::EVRSkeletalMotionRange range, const vr::VRBoneTransform_t* bones,
                                        uint32_t count) {
        const Outer o;
        if (o.first)
            if (OpticalTap* t = g_tap.load(std::memory_order_acquire)) t->OnUpdateSkeleton(h, range, bones, count);
        return orig.updateSkel(self, h, range, bones, count);
    }
};
template <int N> Originals HookSet<N>::orig{};

using PoseUpdatedFn = void (*)(vr::IVRServerDriverHost*, uint32_t, const vr::DriverPose_t&, uint32_t);
constexpr int kSlotPoseUpdated = 1;

template <int N>
struct PoseHook {
    static PoseUpdatedFn orig;

    static void PoseUpdated(vr::IVRServerDriverHost* self, uint32_t device, const vr::DriverPose_t& pose,
                            uint32_t size) {
        orig(self, device, pose, size);
        if (OpticalTap* t = g_tap.load(std::memory_order_acquire)) t->OnPoseUpdated(device, pose, size);
    }
};
template <int N> PoseUpdatedFn PoseHook<N>::orig = nullptr;

constexpr const char* kInterfaceVersions[] = { "IVRDriverInput_005", "IVRDriverInput_004", "IVRDriverInput_003" };
constexpr const char* kHostVersions[] = { "IVRServerDriverHost_006", "IVRServerDriverHost_005" };

template <int N>
int HookVersion(void** vtbl, std::vector<void*>& hooked, bool& skeletonUpdateHooked) {
    int created = 0;
    auto hook = [&](int slot, void* detour, void** original, const char* what) {
        void* target = vtbl[slot];
        if (!target || std::find(hooked.begin(), hooked.end(), target) != hooked.end()) return false;
        const MH_STATUS st = MH_CreateHook(target, detour, original);
        if (st != MH_OK) {
            DriverLog("OpticalTap: MH_CreateHook(%s) failed: %s\n", what, MH_StatusToString(st));
            return false;
        }
        hooked.push_back(target);
        ++created;
        return true;
    };
    hook(kSlotCreateBool, reinterpret_cast<void*>(&HookSet<N>::CreateBool),
         reinterpret_cast<void**>(&HookSet<N>::orig.createBool), "CreateBooleanComponent");
    hook(kSlotUpdateBool, reinterpret_cast<void*>(&HookSet<N>::UpdateBool),
         reinterpret_cast<void**>(&HookSet<N>::orig.updateBool), "UpdateBooleanComponent");
    hook(kSlotCreateScalar, reinterpret_cast<void*>(&HookSet<N>::CreateScalar),
         reinterpret_cast<void**>(&HookSet<N>::orig.createScalar), "CreateScalarComponent");
    hook(kSlotUpdateScalar, reinterpret_cast<void*>(&HookSet<N>::UpdateScalar),
         reinterpret_cast<void**>(&HookSet<N>::orig.updateScalar), "UpdateScalarComponent");
    hook(kSlotCreateSkel, reinterpret_cast<void*>(&HookSet<N>::CreateSkel),
         reinterpret_cast<void**>(&HookSet<N>::orig.createSkel), "CreateSkeletonComponent");
    if (hook(kSlotUpdateSkel, reinterpret_cast<void*>(&HookSet<N>::UpdateSkel),
             reinterpret_cast<void**>(&HookSet<N>::orig.updateSkel), "UpdateSkeletonComponent"))
        skeletonUpdateHooked = true;
    return created;
}

#endif // _WIN32

bool ContainsAny(const std::string& text, const std::vector<std::string>& needles) {
    const std::string t = Lower(text);
    for (const auto& n : needles)
        if (t.find(n) != std::string::npos) return true;
    return false;
}

int TapInputId(const char* name) {
    static constexpr struct { const char* name; int id; } kNames[] = {
        { "/input/system/click", kTapSystem },
        { "/input/index_point/touch", kTapIndexPoint },
        { "/input/index_pinch/value", kTapIndexPinch },
        { "/input/middle_pinch/value", kTapMiddlePinch },
        { "/input/ring_pinch/value", kTapRingPinch },
        { "/input/pinky_pinch/value", kTapPinkyPinch },
        { "/input/grip/value", kTapGrip },
    };
    if (!name) return -1;
    for (const auto& n : kNames)
        if (std::strcmp(name, n.name) == 0) return n.id;
    return -1;
}

} // namespace

OpticalTap::OpticalTap() = default;

OpticalTap::~OpticalTap() { RemoveHooks(); }

void OpticalTap::Configure(const std::string& patternLeft, const std::string& patternRight,
                           const std::string& handTypes, const std::string& ownLeft, const std::string& ownRight,
                           int32_t ownPriority) {
    m_patterns[0] = SplitList(patternLeft);
    m_patterns[1] = SplitList(patternRight);
    m_handTypes = SplitList(handTypes);
    m_own[0] = ownLeft;
    m_own[1] = ownRight;
    m_ownPriority = ownPriority;
}

bool OpticalTap::InstallHooks(vr::IVRDriverContext* ctx) {
#ifndef _WIN32
    (void)ctx;
    DriverLog("OpticalTap: skeleton hook is only supported on Windows\n");
    return false;
#else
    if (m_hooksActive || !ctx) return m_hooksActive;
    const MH_STATUS init = MH_Initialize();
    if (init != MH_OK && init != MH_ERROR_ALREADY_INITIALIZED) {
        DriverLog("OpticalTap: MH_Initialize failed: %s\n", MH_StatusToString(init));
        return false;
    }
    g_tap.store(this, std::memory_order_release);

    std::vector<void*> hooked;
    bool skeletonUpdateHooked = false;
    int created = 0;
    for (int v = 0; v < 3; ++v) {
        vr::EVRInitError err = vr::VRInitError_None;
        void* iface = ctx->GetGenericInterface(kInterfaceVersions[v], &err);
        if (!iface || err != vr::VRInitError_None) {
            DriverLog("OpticalTap: %s not available (err %d)\n", kInterfaceVersions[v], int(err));
            continue;
        }
        void** vtbl = *reinterpret_cast<void***>(iface);
        created += (v == 0)   ? HookVersion<0>(vtbl, hooked, skeletonUpdateHooked)
                   : (v == 1) ? HookVersion<1>(vtbl, hooked, skeletonUpdateHooked)
                              : HookVersion<2>(vtbl, hooked, skeletonUpdateHooked);
    }
    for (int v = 0; v < 2; ++v) {
        vr::EVRInitError err = vr::VRInitError_None;
        void* iface = ctx->GetGenericInterface(kHostVersions[v], &err);
        if (!iface || err != vr::VRInitError_None) {
            DriverLog("OpticalTap: %s not available (err %d)\n", kHostVersions[v], int(err));
            continue;
        }
        void* target = (*reinterpret_cast<void***>(iface))[kSlotPoseUpdated];
        if (!target || std::find(hooked.begin(), hooked.end(), target) != hooked.end()) continue;
        void* detour = (v == 0) ? reinterpret_cast<void*>(&PoseHook<0>::PoseUpdated)
                                : reinterpret_cast<void*>(&PoseHook<1>::PoseUpdated);
        void** original = (v == 0) ? reinterpret_cast<void**>(&PoseHook<0>::orig)
                                   : reinterpret_cast<void**>(&PoseHook<1>::orig);
        const MH_STATUS st = MH_CreateHook(target, detour, original);
        if (st != MH_OK) {
            DriverLog("OpticalTap: MH_CreateHook(TrackedDevicePoseUpdated) failed: %s\n", MH_StatusToString(st));
            continue;
        }
        hooked.push_back(target);
        ++created;
        m_poseHookActive = true;
    }
    if (created == 0) {
        DriverLog("OpticalTap: no hooks created — skeleton tap disabled\n");
        g_tap.store(nullptr);
        MH_Uninitialize();
        return false;
    }
    const MH_STATUS en = MH_EnableHook(MH_ALL_HOOKS);
    if (en != MH_OK) {
        DriverLog("OpticalTap: MH_EnableHook failed: %s\n", MH_StatusToString(en));
        g_tap.store(nullptr);
        MH_Uninitialize();
        m_poseHookActive = false;
        return false;
    }
    m_hooksActive = skeletonUpdateHooked;
    DriverLog("OpticalTap: %d hook(s) installed (skeleton tap %s, pose tap %s)\n", created,
              m_hooksActive ? "active" : "unavailable", m_poseHookActive ? "active" : "unavailable");
    return m_hooksActive;
#endif
}

void OpticalTap::RemoveHooks() {
    if (!g_tap.load()) return;
    g_tap.store(nullptr, std::memory_order_release);
#ifdef _WIN32
    MH_DisableHook(MH_ALL_HOOKS);
    // Let detours already in flight on other threads return through their trampolines.
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    MH_Uninitialize();
#endif
    m_hooksActive = false;
    m_poseHookActive = false;
    DriverLog("OpticalTap: hooks removed\n");
}

// ── registry ───────────────────────────────────────────────────────────────

OpticalTap::SkeletonEntry* OpticalTap::FindSkeleton(vr::VRInputComponentHandle_t h) {
    const int n = m_numSkeletons.load(std::memory_order_acquire);
    for (int i = 0; i < n; ++i)
        if (m_skeletons[i].handle == h) return &m_skeletons[i];
    return nullptr;
}

OpticalTap::SkeletonEntry* OpticalTap::AddSkeleton(vr::VRInputComponentHandle_t h, vr::PropertyContainerHandle_t c,
                                                   int hand, bool orphan, bool baseIsRaw, int level) {
    std::lock_guard<std::mutex> g(m_registerLock);
    if (SkeletonEntry* e = FindSkeleton(h)) return e;
    const int n = m_numSkeletons.load(std::memory_order_relaxed);
    if (n >= kMaxSkeletons) return nullptr;
    SkeletonEntry& e = m_skeletons[n];
    e.handle = h;
    e.container = c;
    e.hand = hand;
    e.orphan = orphan;
    e.baseIsRaw = baseIsRaw;
    e.level = level;
    m_numSkeletons.store(n + 1, std::memory_order_release);
    return &e;
}

std::string OpticalTap::StringOf(vr::PropertyContainerHandle_t c, vr::ETrackedDeviceProperty prop) const {
    if (c == vr::k_ulInvalidPropertyContainer) return {};
    char buf[256]{};
    vr::ETrackedPropertyError err = vr::TrackedProp_Success;
    vr::VRProperties()->GetStringProperty(c, prop, buf, sizeof(buf), &err);
    return (err == vr::TrackedProp_Success) ? std::string(buf) : std::string();
}

bool OpticalTap::IsOwn(const std::string& serial) const {
    return !serial.empty() && (serial == m_own[0] || serial == m_own[1] ||
                               Lower(serial).find("cyberfinger") != std::string::npos);
}

bool OpticalTap::MatchesPattern(const std::string& serial, int hand) const {
    return !serial.empty() && ContainsAny(serial, m_patterns[hand]);
}

bool OpticalTap::IsHandType(const std::string& controllerType) const {
    const std::string t = Lower(controllerType);
    return !t.empty() && std::find(m_handTypes.begin(), m_handTypes.end(), t) != m_handTypes.end();
}

// Never holds e.lock across a vrserver call: the hook threads take the same lock.
void OpticalTap::ResolveSerial(SkeletonEntry& e) {
    {
        std::lock_guard<std::mutex> g(e.lock);
        if (e.serialResolved || e.orphan) return;
    }
    const std::string serial = SerialOf(e.container);   // container is immutable once published
    if (serial.empty()) return;                          // not published yet; retry next frame
    const bool own = IsOwn(serial);
    const std::string type = StringOf(e.container, vr::Prop_ControllerType_String);
    vr::ETrackedPropertyError err = vr::TrackedProp_Success;
    const int32_t priority =
        vr::VRProperties()->GetInt32Property(e.container, vr::Prop_ControllerHandSelectionPriority_Int32, &err);
    {
        std::lock_guard<std::mutex> g(e.lock);
        e.serial = serial;
        e.controllerType = type;
        e.handType = IsHandType(type);
        e.handPriority = (err == vr::TrackedProp_Success) ? priority : 0;
        e.own = own;
        e.serialResolved = true;
    }
    if (own) return;
    if (MatchesPattern(serial, e.hand) || IsHandType(type))
        DriverLog("OpticalTap: %s hand skeleton source '%s' (type %s, tracking level %d, hand priority %d%s)\n",
                  e.hand == 0 ? "left" : "right", serial.c_str(), type.empty() ? "?" : type.c_str(), e.level,
                  (err == vr::TrackedProp_Success) ? priority : 0,
                  e.baseIsRaw ? "" : ", base pose is not /pose/raw");
    else
        DriverLog("OpticalTap: %s hand skeleton of '%s' (type %s) not followed: not a hand-tracking device "
                  "(tap_serial_%s, tap_controller_types)\n", e.hand == 0 ? "left" : "right", serial.c_str(),
                  type.empty() ? "?" : type.c_str(), e.hand == 0 ? "left" : "right");
}

// ── hook callbacks ─────────────────────────────────────────────────────────

// Registers the inputs of interest of every device (ours included); Update()
// only reads those belonging to the selected headset hand device.
void OpticalTap::OnCreateInput(vr::PropertyContainerHandle_t c, const char* name, vr::VRInputComponentHandle_t h) {
    const int id = TapInputId(name);
    if (id < 0) return;
    std::lock_guard<std::mutex> g(m_registerLock);
    const int n = m_numInputs.load(std::memory_order_relaxed);
    for (int i = 0; i < n; ++i)
        if (m_inputs[i].handle == h) return;
    if (n >= kMaxInputs) return;
    m_inputs[n].handle = h;
    m_inputs[n].container = c;
    m_inputs[n].id = id;
    m_numInputs.store(n + 1, std::memory_order_release);
}

// Hot path: every driver's every input update passes through here. True for an update to hold back from SteamVR: a
// hand-tracking source's system button while SetBlockHandSystem is on (its value is recorded all the same).
bool OpticalTap::OnUpdateInput(vr::VRInputComponentHandle_t h, float value) {
    const int n = m_numInputs.load(std::memory_order_acquire);
    for (int i = 0; i < n; ++i) {
        InputEntry& in = m_inputs[i];
        if (in.handle != h) continue;
        in.value.store(value, std::memory_order_relaxed);
        if (in.id != kTapSystem || !m_blockHandSystem.load(std::memory_order_relaxed)) return false;
        int source = in.handSource.load(std::memory_order_relaxed);
        if (source < 0) {
            const std::string type = StringOf(in.container, vr::Prop_ControllerType_String);
            if (type.empty()) return false;                     // not set yet: decide on a later update
            source = IsHandType(type) ? 1 : 0;
            in.handSource.store(source, std::memory_order_relaxed);
        }
        return source == 1;
    }
    return false;
}

void OpticalTap::OnCreateSkeleton(vr::PropertyContainerHandle_t c, const char* /*name*/, const char* skeletonPath,
                                  const char* basePosePath, vr::EVRSkeletalTrackingLevel level,
                                  vr::VRInputComponentHandle_t h) {
    int hand = -1;
    if (skeletonPath && std::strcmp(skeletonPath, "/skeleton/hand/left") == 0) hand = 0;
    if (skeletonPath && std::strcmp(skeletonPath, "/skeleton/hand/right") == 0) hand = 1;
    if (hand < 0) return;
    const bool baseIsRaw = !basePosePath || std::strcmp(basePosePath, "/pose/raw") == 0;
    AddSkeleton(h, c, hand, false, baseIsRaw, int(level));
}

void OpticalTap::OnUpdateSkeleton(vr::VRInputComponentHandle_t h, vr::EVRSkeletalMotionRange range,
                                  const vr::VRBoneTransform_t* bones, uint32_t count) {
    if (!bones || count == 0) return;
    SkeletonEntry* e = FindSkeleton(h);
    if (!e) {
        // Created before our hooks existed: adopt it, keyed by the hand its bones describe.
        const int handed = SkeletonHandedness(bones, count);
        if (handed == 0) return;
        e = AddSkeleton(h, vr::k_ulInvalidPropertyContainer, handed > 0 ? 1 : 0, true, true, 0);
        if (!e) return;
        DriverLog("OpticalTap: adopted %s hand skeleton created before the hooks (%u bones)\n",
                  handed > 0 ? "right" : "left", count);
    }
    bool changed = false;   // new data, not a repeat of the previous update
    {
        std::lock_guard<std::mutex> g(e->lock);
        if (e->own) {
            if (m_capturing.load(std::memory_order_relaxed) && count >= uint32_t(eBone_Count) && e->hand >= 0)
                CaptureSkeleton(e->hand, range == vr::VRSkeletalMotionRange_WithController ? 5 : 4, true, bones);
            return;
        }
        BoneBuffer& buf = (range == vr::VRSkeletalMotionRange_WithController) ? e->with : e->without;
        const uint32_t n = std::min<uint32_t>(count, kMaxBones);
        changed = buf.count != count || std::memcmp(buf.bones, bones, n * sizeof(vr::VRBoneTransform_t)) != 0;
        std::memcpy(buf.bones, bones, n * sizeof(vr::VRBoneTransform_t));
        buf.count = count;
        buf.time = NowSeconds();
        e->lastUpdate = buf.time;
    }
    // The selected source of a hand: hand the update on as it happens (no lock held).
    const int hand = e->hand;
    if (hand < 0 || m_selected[hand].load(std::memory_order_acquire) != int(e - m_skeletons)) return;
    if (range == vr::VRSkeletalMotionRange_WithoutController) {
        m_skeletonEvents[hand].fetch_add(1, std::memory_order_relaxed);
        if (changed) m_skeletonNew[hand].fetch_add(1, std::memory_order_relaxed);
    }
    if (m_capturing.load(std::memory_order_relaxed) && count >= uint32_t(eBone_Count))
        CaptureSkeleton(hand, range == vr::VRSkeletalMotionRange_WithController ? 2 : 1, changed, bones);
    if (TapListener* l = m_listener.load(std::memory_order_acquire)) l->OnTapSkeleton(hand, range, bones, count);
}

void OpticalTap::OnPoseUpdated(uint32_t device, const vr::DriverPose_t& pose, uint32_t size) {
    if (size < sizeof(vr::DriverPose_t) || device == vr::k_unTrackedDeviceIndexInvalid) return;
    if (m_capturing.load(std::memory_order_relaxed))
        for (int hand = 0; hand < 2; ++hand)
            if (m_ownDevice[hand].load(std::memory_order_relaxed) == device) {
                CapturePose(hand, 3, true, pose);
                return;
            }
    for (int hand = 0; hand < 2; ++hand) {
        if (m_sourceDevice[hand].load(std::memory_order_acquire) != device) continue;
        m_poseEvents[hand].fetch_add(1, std::memory_order_relaxed);
        bool changed = false;
        {
            LastPose& last = m_lastPose[hand];
            std::lock_guard<std::mutex> g(last.lock);
            if (std::memcmp(last.position, pose.vecPosition, sizeof(last.position)) != 0 ||
                std::memcmp(&last.rotation, &pose.qRotation, sizeof(last.rotation)) != 0) {
                changed = true;
                m_poseNew[hand].fetch_add(1, std::memory_order_relaxed);
                std::memcpy(last.position, pose.vecPosition, sizeof(last.position));
                last.rotation = pose.qRotation;
            }
        }
        if (m_capturing.load(std::memory_order_relaxed)) CapturePose(hand, 0, changed, pose);
        if (TapListener* l = m_listener.load(std::memory_order_acquire)) l->OnTapPose(hand, pose);
    }
}

void OpticalTap::Capture(const CaptureRecord& r) {
    std::lock_guard<std::mutex> g(m_captureLock);
    if (m_capture.size() < 800000) m_capture.push_back(r);
}

// poseTimeOffset, position, rotation (w x y z), velocity, angular velocity, acceleration, valid, result, and two
// fields of the driver-from-world transform.
void OpticalTap::CapturePose(int hand, uint8_t kind, bool changed, const vr::DriverPose_t& pose) {
    CaptureRecord r{};
    r.t = NowSeconds();
    r.hand = uint8_t(hand);
    r.kind = kind;
    r.changed = changed;
    const double v[21] = { pose.poseTimeOffset, pose.vecPosition[0], pose.vecPosition[1], pose.vecPosition[2],
                           pose.qRotation.w, pose.qRotation.x, pose.qRotation.y, pose.qRotation.z,
                           pose.vecVelocity[0], pose.vecVelocity[1], pose.vecVelocity[2],
                           pose.vecAngularVelocity[0], pose.vecAngularVelocity[1], pose.vecAngularVelocity[2],
                           pose.vecAcceleration[0], pose.vecAcceleration[1], pose.vecAcceleration[2],
                           pose.poseIsValid ? 1.0 : 0.0, double(pose.result),
                           pose.qWorldFromDriverRotation.w, pose.vecWorldFromDriverTranslation[1] };
    for (int i = 0; i < 21; ++i) r.v[i] = float(v[i]);
    Capture(r);
}

// CyberFinger IMU: `changed` holds the slot bits; quaternions of body 1, body 2, joint (w x y z), raw accel of each
// (x y z), and the delay from the report's BLE arrival at the bridge to its arrival here (the bridge's clock,
// time.perf_counter(), reads the same performance counter as NowSeconds()).
void OpticalTap::CaptureImu(const ImuPacket& p, double arrival) {
    if (!m_capturing.load(std::memory_order_relaxed)) return;
    CaptureRecord r{};
    r.t = arrival;
    r.hand = p.h.hand & 1;
    r.kind = 6;
    r.changed = p.present;
    for (int s = 0; s < 3; ++s)
        for (int i = 0; i < 4; ++i) r.v[4 * s + i] = p.quat[s][i];
    for (int s = 0; s < 3; ++s)
        for (int i = 0; i < 3; ++i) r.v[12 + 3 * s + i] = p.has_accel ? float(p.accel[s][i]) : 0.f;
    r.v[21] = float(arrival - (double(p.h.t_send_us) - double(p.h.age_us)) * 1e-6);
    Capture(r);
}

void OpticalTap::CaptureDevicePose(uint8_t kind, uint8_t id, const vr::TrackedDevicePose_t& pose, double now) {
    if (!m_capturing.load(std::memory_order_relaxed)) return;
    CaptureRecord r{};
    r.t = now;
    r.hand = id;
    r.kind = kind;
    r.changed = pose.bPoseIsValid ? 1 : 0;
    const Xform x = XformFromMatrix(pose.mDeviceToAbsoluteTracking);
    const double v[18] = { 0.0, x.p.x, x.p.y, x.p.z, x.q.w, x.q.x, x.q.y, x.q.z,        // as a hand pose (kind 0)
                           pose.vVelocity.v[0], pose.vVelocity.v[1], pose.vVelocity.v[2],
                           pose.vAngularVelocity.v[0], pose.vAngularVelocity.v[1], pose.vAngularVelocity.v[2],
                           0.0, 0.0, 0.0, pose.bPoseIsValid ? 1.0 : 0.0 };
    for (int i = 0; i < 18; ++i) r.v[i] = float(v[i]);
    Capture(r);
}

// Wrist in model space (root · wrist: the pose → wrist transform), then the five fingertips relative to the wrist.
void OpticalTap::CaptureSkeleton(int hand, uint8_t kind, bool changed, const vr::VRBoneTransform_t* bones) {
    CaptureRecord r{};
    r.t = NowSeconds();
    r.hand = uint8_t(hand);
    r.kind = kind;
    r.changed = changed;
    Xform model[eBone_Count];
    ModelSpace(bones, model);
    const Xform& wrist = model[eBone_Wrist];
    const float w[7] = { float(wrist.p.x), float(wrist.p.y), float(wrist.p.z), float(wrist.q.w),
                         float(wrist.q.x), float(wrist.q.y), float(wrist.q.z) };
    std::copy(w, w + 7, r.v);
    const Xform inv = Inverse(wrist);
    static constexpr int kTips[5] = { eBone_Thumb3, eBone_IndexFinger4, eBone_MiddleFinger4, eBone_RingFinger4,
                                      eBone_PinkyFinger4 };
    for (int f = 0; f < 5; ++f) {
        const Vec3 p = (inv * model[kTips[f]]).p;
        r.v[7 + 3 * f] = float(p.x);
        r.v[8 + 3 * f] = float(p.y);
        r.v[9 + 3 * f] = float(p.z);
    }
    Capture(r);
}

void OpticalTap::StartCapture(double seconds, const std::string& path) {
    {
        std::lock_guard<std::mutex> g(m_captureLock);
        m_capture.clear();
        m_capture.reserve(size_t(seconds * 2 * 6 * 400) + 1000);   // 2 hands x (source + ours) x 3 kinds
    }
    m_capturePath = path;
    m_captureEnd = NowSeconds() + seconds;
    m_capturing.store(true, std::memory_order_release);
    DriverLog("OpticalTap: capturing the source hands and our devices for %.0f s\n", seconds);
}

void OpticalTap::PollCapture(double now) {
    if (!m_capturing.load(std::memory_order_relaxed) || now < m_captureEnd) return;
    m_capturing.store(false, std::memory_order_release);
    std::vector<CaptureRecord> records;
    {
        std::lock_guard<std::mutex> g(m_captureLock);
        records.swap(m_capture);
    }
    // Written off the frame loop so the file I/O can't stall vrserver.
    std::thread([records = std::move(records), path = m_capturePath]() {
        std::FILE* f = std::fopen(path.c_str(), "w");
        if (!f) {
            DriverLog("OpticalTap: cannot write capture %s\n", path.c_str());
            return;
        }
        std::fprintf(f, "t,hand,kind,changed");
        for (int i = 0; i < 22; ++i) std::fprintf(f, ",v%d", i);
        std::fprintf(f, "\n");
        for (const CaptureRecord& r : records) {
            std::fprintf(f, "%.6f,%u,%u,%u", r.t, unsigned(r.hand), unsigned(r.kind), unsigned(r.changed));
            for (int i = 0; i < 22; ++i) std::fprintf(f, ",%.7g", r.v[i]);
            std::fprintf(f, "\n");
        }
        std::fclose(f);
        DriverLog("OpticalTap: capture written: %s (%zu updates)\n", path.c_str(), records.size());
    }).detach();
}

OpticalTap::Rates OpticalTap::TakeRates(double seconds, int hand) {
    const double s = seconds > 0 ? seconds : 1.0;
    Rates r;
    r.pose = m_poseEvents[hand].exchange(0, std::memory_order_relaxed) / s;
    r.poseNew = m_poseNew[hand].exchange(0, std::memory_order_relaxed) / s;
    r.skeleton = m_skeletonEvents[hand].exchange(0, std::memory_order_relaxed) / s;
    r.skeletonNew = m_skeletonNew[hand].exchange(0, std::memory_order_relaxed) / s;
    return r;
}

// ── per-frame snapshot ─────────────────────────────────────────────────────

vr::TrackedDeviceIndex_t OpticalTap::DeviceForContainer(vr::PropertyContainerHandle_t c, uint32_t count) const {
    if (c == vr::k_ulInvalidPropertyContainer) return vr::k_unTrackedDeviceIndexInvalid;
    for (uint32_t i = 0; i < count; ++i)
        if (vr::VRProperties()->TrackedDeviceToPropertyContainer(i) == c) return i;
    return vr::k_unTrackedDeviceIndexInvalid;
}

// A headset hand device for this hand, by serial pattern or by hand-tracking controller type + role hint.
vr::TrackedDeviceIndex_t OpticalTap::DeviceForPattern(int hand, uint32_t count) const {
    const int32_t role = hand ? vr::TrackedControllerRole_RightHand : vr::TrackedControllerRole_LeftHand;
    for (uint32_t i = 1; i < count; ++i) {
        const vr::PropertyContainerHandle_t c = vr::VRProperties()->TrackedDeviceToPropertyContainer(i);
        const std::string serial = SerialOf(c);
        if (serial.empty() || IsOwn(serial)) continue;
        if (MatchesPattern(serial, hand)) return i;
        vr::ETrackedPropertyError err = vr::TrackedProp_Success;
        if (IsHandType(StringOf(c, vr::Prop_ControllerType_String)) &&
            vr::VRProperties()->GetInt32Property(c, vr::Prop_ControllerRoleHint_Int32, &err) == role &&
            err == vr::TrackedProp_Success)
            return i;
    }
    return vr::k_unTrackedDeviceIndexInvalid;
}

void OpticalTap::Update(const vr::TrackedDevicePose_t* poses, uint32_t count) {
    const double now = NowSeconds();
    const int n = m_numSkeletons.load(std::memory_order_acquire);

    for (int i = 0; i < n; ++i) ResolveSerial(m_skeletons[i]);

    for (int hand = 0; hand < 2; ++hand) {
        TapHandSnapshot s;

        // Pick the skeleton source: a known hand device (serial pattern or hand-tracking controller type)
        // > full tracking level > freshest.
        int best = -1;
        double bestScore = -1;
        for (int i = 0; i < n; ++i) {
            SkeletonEntry& e = m_skeletons[i];
            if (e.hand != hand) continue;
            std::lock_guard<std::mutex> g(e.lock);
            if (e.own || (!e.orphan && !e.serialResolved)) continue;
            // Only hand tracking stands in for the hand. A controller's skeleton (the fingers Steam Link's Touch
            // controllers estimate from their sensors) streams on while the controller is in use: following it, the
            // handoff never sees the hand lost, so CyberFinger keeps the hand and hides the controller from apps.
            // Adopted skeletons (Virtual Desktop's, created before the hooks) carry no device to tell by.
            if (!e.orphan && !MatchesPattern(e.serial, hand) && !e.handType) continue;
            double score = 0;
            if (!e.orphan && (MatchesPattern(e.serial, hand) || e.handType)) score += 8;
            if (e.level == vr::VRSkeletalTracking_Full) score += 4;
            if (!e.orphan) score += 2;
            if (now - e.lastUpdate < 1.0) score += 1;
            score += 1e-6 * std::max(0.0, 1e3 - (now - e.lastUpdate));   // tie-break: freshest
            if (score > bestScore) { bestScore = score; best = i; }
        }
        if (best != m_selected[hand].load(std::memory_order_relaxed)) {
            m_selected[hand].store(best, std::memory_order_release);
            if (best >= 0) {
                std::string who, type;
                int32_t priority = 0;
                {
                    std::lock_guard<std::mutex> g(m_skeletons[best].lock);
                    who = m_skeletons[best].orphan ? "an adopted skeleton" : m_skeletons[best].serial;
                    type = m_skeletons[best].controllerType;
                    priority = m_skeletons[best].handPriority;
                }
                DriverLog("OpticalTap: %s hand now follows %s%s%s\n", hand == 0 ? "left" : "right", who.c_str(),
                          type.empty() ? "" : ", type ", type.c_str());
                if (priority >= m_ownPriority)
                    DriverLog("OpticalTap: %s declares hand priority %d >= CyberFinger's %d: it may keep the hand role; "
                              "raise hand_selection_priority\n", who.c_str(), priority, m_ownPriority);
            }
        }

        vr::TrackedDeviceIndex_t dev = vr::k_unTrackedDeviceIndexInvalid;
        vr::PropertyContainerHandle_t container = vr::k_ulInvalidPropertyContainer;
        if (best >= 0) {
            SkeletonEntry& e = m_skeletons[best];
            if (!e.orphan) {
                // Device index for the source's container (resolved outside the entry lock).
                container = e.container;
                vr::TrackedDeviceIndex_t cached;
                {
                    std::lock_guard<std::mutex> g(e.lock);
                    cached = e.device;
                }
                const bool stale = cached >= count || !poses[cached].bDeviceIsConnected ||
                                   vr::VRProperties()->TrackedDeviceToPropertyContainer(cached) != container;
                if (stale) {
                    cached = DeviceForContainer(container, count);
                    std::lock_guard<std::mutex> g(e.lock);
                    e.device = cached;
                }
                dev = cached;
            }
            std::lock_guard<std::mutex> g(e.lock);
            const BoneBuffer* src = nullptr;
            if (now - e.without.time < 0.5) src = &e.without;
            else if (now - e.with.time < 0.5) src = &e.with;
            else src = (e.without.time >= e.with.time) ? &e.without : &e.with;
            if (src->count > 0) {
                s.boneCount = src->count;
                s.skeletonAge = now - src->time;
                s.skeletonValid = s.skeletonAge < 1.0 && src->count >= uint32_t(eBone_Count);
                std::memcpy(s.bones, src->bones, std::min<uint32_t>(src->count, eBone_Count) * sizeof(vr::VRBoneTransform_t));
            }
            s.baseIsRaw = e.baseIsRaw;
            s.serial = e.orphan ? std::string("(adopted)") : e.serial;
            s.controllerType = e.controllerType;
        }

        // No registered source: find the device by serial pattern (pose only).
        if (dev == vr::k_unTrackedDeviceIndexInvalid) {
            vr::TrackedDeviceIndex_t& cached = m_patternDevice[hand];
            if (cached >= count || !poses[cached].bDeviceIsConnected) {
                cached = vr::k_unTrackedDeviceIndexInvalid;
                if (now >= m_nextPatternSearch[hand]) {
                    m_nextPatternSearch[hand] = now + 1.0;
                    cached = DeviceForPattern(hand, count);
                }
            }
            dev = cached;
            if (dev != vr::k_unTrackedDeviceIndexInvalid) {
                container = vr::VRProperties()->TrackedDeviceToPropertyContainer(dev);
                if (s.serial.empty() || s.serial == "(adopted)") s.serial = SerialOf(container);
                if (s.controllerType.empty()) s.controllerType = StringOf(container, vr::Prop_ControllerType_String);
            }
        }

        m_sourceDevice[hand].store(dev < count ? dev : vr::k_unTrackedDeviceIndexInvalid, std::memory_order_release);
        if (dev != vr::k_unTrackedDeviceIndexInvalid && dev < count) {
            const vr::TrackedDevicePose_t& p = poses[dev];
            s.sourceFound = p.bDeviceIsConnected;
            s.poseValid = p.bDeviceIsConnected && p.bPoseIsValid;
            s.trackingResult = uint8_t(p.eTrackingResult);
            s.rawPose = XformFromMatrix(p.mDeviceToAbsoluteTracking);
            s.linVel = { p.vVelocity.v[0], p.vVelocity.v[1], p.vVelocity.v[2] };
            s.angVel = { p.vAngularVelocity.v[0], p.vAngularVelocity.v[1], p.vAngularVelocity.v[2] };
        }

        if (container != vr::k_ulInvalidPropertyContainer) {
            const int ni = m_numInputs.load(std::memory_order_acquire);
            for (int i = 0; i < ni; ++i) {
                const InputEntry& in = m_inputs[i];
                if (in.container != container) continue;
                s.input[in.id] = in.value.load(std::memory_order_relaxed);
                if (in.id >= kTapIndexPinch) s.gesturesPresent = true;
            }
            s.systemClick = s.input[kTapSystem] > 0.5f;
        }

        MeasureNoise(hand, s, now);
        std::lock_guard<std::mutex> g(m_snapLock);
        m_snap[hand] = std::move(s);
    }
}

TapHandSnapshot OpticalTap::Get(int hand) const {
    std::lock_guard<std::mutex> g(m_snapLock);
    return m_snap[hand & 1];
}

// Feeds the still-hand noise meters from the frame's snapshot of the source hand.
void OpticalTap::MeasureNoise(int hand, const TapHandSnapshot& s, double now) {
    if (!s.poseValid || !s.skeletonValid || s.boneCount < uint32_t(eBone_Count)) return;
    static constexpr int kTips[5] = { eBone_Thumb3, eBone_IndexFinger4, eBone_MiddleFinger4, eBone_RingFinger4,
                                      eBone_PinkyFinger4 };
    Xform model[eBone_Count];
    ModelSpace(s.bones, model);
    NoiseMeters& m = m_noise[hand];
    const Xform wrist = s.rawPose * model[eBone_Wrist];
    m.wrist.Add(wrist.p, now);
    const Xform wristInv = Inverse(model[eBone_Wrist]);
    for (int f = 0; f < 5; ++f) {
        m.tips[f].Add((s.rawPose * model[kTips[f]]).p, now);
        m.fingers[f].Add((wristInv * model[kTips[f]]).p, now);
    }
}

OpticalTap::Noise OpticalTap::TakeNoise(int hand) {
    NoiseMeters& m = m_noise[hand & 1];
    Noise n;
    n.wrist = m.wrist.Take(n.windows);
    // RMS over the fingertips that were held still at all (a meter without still windows reads 0).
    double tips = 0, fingers = 0;
    int nTips = 0, nFingers = 0;
    for (int f = 0; f < 5; ++f) {
        int wt = 0, wf = 0;
        const double t = m.tips[f].Take(wt), g = m.fingers[f].Take(wf);
        if (wt > 0) { tips += t * t; ++nTips; }
        if (wf > 0) { fingers += g * g; ++nFingers; }
    }
    n.tips = nTips ? std::sqrt(tips / nTips) : 0.0;
    n.fingers = nFingers ? std::sqrt(fingers / nFingers) : 0.0;
    return n;
}

} // namespace cf
