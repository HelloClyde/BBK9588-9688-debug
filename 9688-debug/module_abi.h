/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9688_MODULE_ABI_H
#define DEBUG9688_MODULE_ABI_H
#include <stdint.h>
typedef struct {
    uint32_t version,size;
    uint32_t (*tick)(void);
    int (*poll_cancel)(void);
    uint32_t (*runtime_table)(unsigned index);
} debug9688_api;
typedef struct {
    uint32_t opcode;
    const uint8_t *args;
    uint32_t args_size;
    uint8_t *output;
    uint32_t output_capacity,output_size;
} debug9688_call;
/* MIPS32/O32/little endian/soft float. Same 32 KiB stack as debug worker.
 * Call poll_cancel regularly; it yields and serves USB. No fault isolation. */
int debug9688_entry(const debug9688_api *,debug9688_call *);
#endif
