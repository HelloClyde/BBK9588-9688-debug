/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9588_FIRMWARE_H
#define DEBUG9588_FIRMWARE_H
#include "debug.h"
#ifdef RD_UNIVERSAL_LAUNCHER
#include "launcher_profile_generated.h"
#else
#include "profile_generated.h"
#if RD_SOC_JZ4720 + RD_SOC_JZ4730 + RD_SOC_JZ4740 != 1
#error "exactly one checked resident SoC is required"
#endif
#endif
/* Screen dimensions and all call limits must come from a checked 9588 profile. */
int fw_matches(void);
uint32_t fw_selected_soc(void);
uint32_t fw_match_error(void);
int fw_notice_safe(void);
uint32_t fw_tick(void);
void fw_delay(uint32_t ticks);
int fw_wake_install(rd_header *control);
int fw_wake_remove(void);
int fw_task_create(rd_header *control);
rd_header *fw_find_resident(void);
rd_header *fw_task_control(void);
int fw_task_slot_empty(void);
void *fw_alloc(uint32_t bytes);
void fw_free(void *pointer);
#endif
