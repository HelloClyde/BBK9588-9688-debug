/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9688_FIRMWARE_H
#define DEBUG9688_FIRMWARE_H
#include "debug.h"
#ifdef RD_UNIVERSAL_LAUNCHER
#include "launcher_profile_generated.h"
#else
#include "profile_generated.h"
#endif
#ifndef FW_SCREEN_WIDTH
#define FW_SCREEN_WIDTH 240u
#endif
#ifndef FW_SCREEN_HEIGHT
#define FW_SCREEN_HEIGHT 320u
#endif
#ifndef FW_GUI_CALL_LIMIT
#define FW_GUI_CALL_LIMIT 0x820u
#define FW_FS_CALL_LIMIT 0x84u
#define FW_SYS_CALL_LIMIT 0xccu
#define FW_MEM_CALL_LIMIT 0x30u
#define FW_RES_CALL_LIMIT 0x9cu
#endif
int fw_matches(void);
uint32_t fw_binding_id(void);
unsigned fw_discovery_info(uint32_t *out,unsigned capacity);
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
