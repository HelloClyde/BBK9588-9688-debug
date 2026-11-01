/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9588_MODULE_ABI_H
#define DEBUG9588_MODULE_ABI_H
#include <stdint.h>
typedef struct {
    uint32_t version,size;
    uint32_t (*tick)(void);
    int (*poll_cancel)(void);
    uint32_t (*runtime_table)(unsigned index);
} debug9588_api;
typedef struct {
    uint32_t opcode;
    const uint8_t *args;
    uint32_t args_size;
    uint8_t *output;
    uint32_t output_capacity,output_size;
} debug9588_call;
/* MIPS32/O32/little endian/soft float. Same 32 KiB stack as debug worker.
 * Call poll_cancel regularly; it yields and serves USB. No fault isolation. */
int debug9588_entry(const debug9588_api *,debug9588_call *);
#endif
