/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef RD_MANAGEMENT_H
#define RD_MANAGEMENT_H
#include <stdint.h>
#define RD_MGMT_FILES 1u
#define RD_MGMT_CRC 2u
#define RD_MGMT_WINDOWS 4u
#define RD_MGMT_REFRESH 8u
#define RD_MGMT_LAUNCH 16u
#define RD_MGMT_INPUT 32u
unsigned rd_management_command(unsigned,const uint8_t *,unsigned,uint8_t *,unsigned *);
void rd_management_step(void);
void rd_management_disconnect(void);
void rd_management_reset(void);
void rd_management_shutdown(void);
int rd_management_busy(void);
int rd_management_input_pending(void);
#endif
