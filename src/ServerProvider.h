/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// ServerProvider.h — IServerTrackedDeviceProvider implementation
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <memory>
#include "MergedController.h"
#include "HandTrackingReceiver.h"

namespace merged_ctrl {

class ServerProvider : public vr::IServerTrackedDeviceProvider {
public:
    // ── IServerTrackedDeviceProvider ───────────────────────────────────
    vr::EVRInitError Init(vr::IVRDriverContext* pDriverContext) override;
    void Cleanup() override;
    const char* const* GetInterfaceVersions() override;
    void RunFrame() override;
    bool ShouldBlockStandbyMode() override;
    void EnterStandby() override;
    void LeaveStandby() override;

private:
    std::unique_ptr<HandTrackingReceiver>  m_handTrackingReceiver;
    std::unique_ptr<MergedController>      m_leftController;
    std::unique_ptr<MergedController>      m_rightController;
};

} // namespace merged_ctrl

