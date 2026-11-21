# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

# The CyberFinger driver for WiVRn is compiled into wivrn-server by WiVRn's own build (build.sh, with
# wivrn-cyberfinger.patch). This project only builds its tests, which need neither WiVRn nor a headset:
#
#   cmake -B build bridge_linux/wivrn && cmake --build build && ctest --test-dir build --output-on-failure
cmake_minimum_required(VERSION 3.16)
project(cyberfinger_wivrn_tests LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(CF_SRC "${CMAKE_CURRENT_SOURCE_DIR}/../../src")
find_package(nlohmann_json REQUIRED)
find_package(Threads REQUIRED)

# The driver as WiVRn builds it, less CfWivrn.cpp (WiVRn's types).
add_library(cyberfinger_driver STATIC
    driver/CfConfig.cpp
    driver/CfDriver.cpp
    driver/CfHand.cpp
    "${CF_SRC}/SkeletonSynth.cpp"
    "${CF_SRC}/StudioLink.cpp"
)
target_include_directories(cyberfinger_driver PUBLIC driver "${CF_SRC}")
target_compile_definitions(cyberfinger_driver PUBLIC CF_NO_OPENVR)
target_compile_options(cyberfinger_driver PRIVATE -Wall -Wextra)
target_link_libraries(cyberfinger_driver PUBLIC nlohmann_json::nlohmann_json Threads::Threads)

enable_testing()
add_executable(wivrn_driver_tests tests/wivrn_driver_tests.cpp)
target_compile_options(wivrn_driver_tests PRIVATE -Wall -Wextra)
target_link_libraries(wivrn_driver_tests PRIVATE cyberfinger_driver)
add_test(NAME wivrn_driver_tests COMMAND wivrn_driver_tests "${CMAKE_CURRENT_BINARY_DIR}")

# The driver on its UDP ports with made-up hands, to try the bridge against without a headset.
add_executable(wivrn_driver_sim tests/wivrn_driver_sim.cpp)
target_compile_options(wivrn_driver_sim PRIVATE -Wall -Wextra)
target_link_libraries(wivrn_driver_sim PRIVATE cyberfinger_driver)

# The settings section the WiVRn dashboard gets (dashboard/), driven without the dashboard. Needs Qt 6 and KDE
# Frameworks, as the dashboard does; skipped without them.
find_package(Qt6 QUIET COMPONENTS Quick QuickControls2 Widgets Test)
find_package(KF6I18n QUIET)
find_package(KF6Kirigami QUIET)
if(Qt6_FOUND AND KF6I18n_FOUND AND KF6Kirigami_FOUND)
    set(CMAKE_AUTOMOC ON)
    add_executable(dashboard_settings_test
        tests/dashboard_settings_test.cpp
        dashboard/cyberfinger_config.cpp
        dashboard/cyberfinger_config.h
    )
    target_include_directories(dashboard_settings_test PRIVATE dashboard)
    target_link_libraries(dashboard_settings_test PRIVATE cyberfinger_driver
        Qt6::Quick Qt6::QuickControls2 Qt6::Widgets Qt6::Test KF6::I18n KF6::I18nQml)
    add_test(NAME dashboard_settings_test
        COMMAND dashboard_settings_test "${CMAKE_CURRENT_SOURCE_DIR}/tests" "${CMAKE_CURRENT_BINARY_DIR}")
    set_tests_properties(dashboard_settings_test PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software")
else()
    message(STATUS "Qt 6 / KDE Frameworks not found: the dashboard settings test is skipped")
endif()
