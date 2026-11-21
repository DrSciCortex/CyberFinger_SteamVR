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
#include <cstring>
#include "ServerProvider.h"

static cf::ServerProvider g_serverProvider;

#if defined(_WIN32)
#  define DLLEXPORT extern "C" __declspec(dllexport)
#else
#  define DLLEXPORT extern "C" __attribute__((visibility("default")))
#endif

DLLEXPORT void* HmdDriverFactory(const char* pInterfaceName, int* pReturnCode) {
    if (std::strcmp(pInterfaceName, vr::IServerTrackedDeviceProvider_Version) == 0)
        return &g_serverProvider;
    if (pReturnCode) *pReturnCode = vr::VRInitError_Init_InterfaceNotFound;
    return nullptr;
}
