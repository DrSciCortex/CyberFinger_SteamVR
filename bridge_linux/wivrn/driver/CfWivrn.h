/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// CfWivrn.h — the CyberFinger driver's face toward WiVRn's server
//
// WiVRn already has a controller device per hand, which also carries the
// headset's hand joints. The patch (../wivrn-cyberfinger.patch) gives each a
// WivrnHand and calls it where the device reads its inputs, hands out poses
// and joints, and vibrates; the session asks WivrnDriver, with every tracking
// packet, whose the hand is. While a CyberFinger holds a hand, that device
// keeps the hand's role and applications see a controller (Profile) at the
// tracked hand, with the CyberFinger's buttons.
// ═══════════════════════════════════════════════════════════════════════════

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <vector>

#include "xrt/xrt_defines.h"
#include "xrt/xrt_device.h"

namespace wivrn {
struct clock_offset;
namespace from_headset {
struct tracking;
struct hand_tracking;
} // namespace from_headset
} // namespace wivrn

namespace cf {

class Driver;
class Hand;

class WivrnHand {
public:
    // The headset's grip pose for the time a pose or the joints are asked for; called only while a CyberFinger
    // holds the hand.
    using GripPose = std::function<xrt_space_relation()>;

    // xrt_device::update_inputs: the CyberFinger's inputs over the device's own, while it holds the hand.
    void UpdateInputs(xrt_input* inputs, size_t count);
    // xrt_device::get_tracked_pose, on its result.
    void AdjustPose(xrt_input_name name, int64_t targetNs, xrt_space_relation& pose, const GripPose& grip);
    // xrt_device::get_hand_tracking, on its result.
    void AdjustJoints(int64_t targetNs, xrt_hand_joint_set& joints, const GripPose& grip);
    // xrt_device::set_output: true if the CyberFinger took the vibration.
    bool Haptic(const xrt_output_value& value);

    // True once each time the CyberFinger takes the hand (network thread, before OnTracking): the device then
    // goes by the headset's own aim, pinch and poke poses for the hand, not a controller's derived ones.
    bool TakeActivation();
    // The headset's packets, from the network thread.
    void OnTracking(const wivrn::from_headset::tracking& tracking, const wivrn::clock_offset& offset);
    void OnHandTracking(const wivrn::from_headset::hand_tracking& tracking, const wivrn::clock_offset& offset);

private:
    friend class WivrnDriver;
    Driver* m_driver = nullptr;
    Hand*   m_hand = nullptr;
    int     m_index = 0;
    bool    m_wasActive = false;      // network thread
    std::atomic<double> m_lastQuery{ -1e9 };   // an application last asked for a pose or the joints
    struct Last { float x = 0, y = 0; int64_t changed = 0; };
    std::vector<Last> m_last;         // per input: its value and when it changed (the input's timestamp)
};

class WivrnDriver {
public:
    WivrnDriver();
    ~WivrnDriver();

    WivrnHand& GetHand(int hand) { return m_hands[hand & 1]; }

    // The controller the hand's role reports: the emulated one while a CyberFinger holds the hand, else what the
    // headset says is there (fromHeadset; hand interaction, a controller, or nothing).
    xrt_device_name Profile(int hand, xrt_device_name fromHeadset);

    // The headset only sends the poses and joints applications ask for. True while a CyberFinger holds the hand
    // and none does: the session then asks itself, so the IMU fusion calibrates and the bridge shows the hands
    // before an application starts.
    bool KeepTracking(int hand) const;

private:
    std::unique_ptr<Driver> m_driver;
    WivrnHand m_hands[2];
    std::atomic<uint32_t> m_profileGeneration{ 0 };
    std::atomic<xrt_device_name> m_profile{ XRT_DEVICE_TOUCH_CONTROLLER };
};

} // namespace cf
