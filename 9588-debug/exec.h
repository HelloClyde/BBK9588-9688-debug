/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9588_EXEC_H
#define DEBUG9588_EXEC_H
#include "module_abi.h"
#define EXEC_BYTES 65536u
enum { EXEC_EMPTY,EXEC_LOADING,EXEC_READY,EXEC_PENDING,EXEC_RUNNING,EXEC_DONE };
extern uint32_t exec_state;
unsigned rd_exec_command(unsigned,const uint8_t *,unsigned,uint8_t *,unsigned *);
void rd_exec_reset(void);
void rd_exec_cancel(unsigned);
void rd_exec_invoke(const debug9588_api *);
int rd_exec_poll_flags(void);
#endif
