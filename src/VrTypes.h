/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// VrTypes.h — the OpenVR types the portable code is written in
//
// The SteamVR driver gets them from the OpenVR SDK. The WiVRn driver
// (bridge_linux/wivrn) builds the same sources inside an OpenXR runtime, where
// there is no OpenVR: with CF_NO_OPENVR defined, the few types the skeleton and
// gesture code needs are declared here, with OpenVR's layout.
// ═══════════════════════════════════════════════════════════════════════════

#ifndef CF_NO_OPENVR
#include <openvr_driver.h>
#else
#include <cstdint>

namespace vr {

struct HmdVector4_t { float v[4]; };
struct HmdQuaternionf_t { float w, x, y, z; };
struct VRBoneTransform_t {
    HmdVector4_t position;
    HmdQuaternionf_t orientation;
};

} // namespace vr
#endif
