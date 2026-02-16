/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// driver_main.cpp — SteamVR driver entry point
//
// Exports HmdDriverFactory() which SteamVR calls to get our provider.
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include "ServerProvider.h"

static merged_ctrl::ServerProvider g_serverProvider;

// ── DLL Export ─────────────────────────────────────────────────────────────
#if defined(_WIN32)
#  define DLLEXPORT extern "C" __declspec(dllexport)
#else
#  define DLLEXPORT extern "C" __attribute__((visibility("default")))
#endif

DLLEXPORT void* HmdDriverFactory(const char* pInterfaceName,
                                  int* pReturnCode) {
    if (std::string(pInterfaceName) == vr::IServerTrackedDeviceProvider_Version) {
        return &g_serverProvider;
    }

    if (pReturnCode)
        *pReturnCode = vr::VRInitError_Init_InterfaceNotFound;
    return nullptr;
}

