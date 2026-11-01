/* SPDX-License-Identifier: GPL-3.0-only
 * Adapted from BBK H1 h1-debug/usb.h (BBKH1-debug, GPL-3.0-only).
 * 9588 profile-selected firmware transport; board/firmware binding lives in firmware.h.
 */
#ifndef BBK9588_DEBUG_USB_H
#define BBK9588_DEBUG_USB_H
#include <stdint.h>
#include "firmware.h"

/* Full-speed CDC ACM on the selected JZ4730/JZ4740. The caller serializes
 * operations and provides bounded polling while this driver owns the UDC.
 * firmware.h must define the verified profile's SoC register addresses,
 * USB gate/PHY bits and board cable GPIO masks, plus fw_matches/tick/delay.
 * The native USB task and cable IRQ must remain masked until close().
 */
typedef struct {
    uint32_t clock_gate,opcr,imr,pdmask,pdflag;
    uint16_t txe,rxe,maxp[3][2],csr[3][2];
    uint8_t power,index,usbe,address,owned,configured,control_phase,reset_pending;
    uint8_t control[128],line[7],pending_address;
    uint16_t control_length,control_offset;
    uint32_t resets,setups,stalls,rx_packets,tx_packets,ownership_fault;
    uint32_t fault_reason,fault_imr,fault_pdmask,fault_pin;
    uint32_t reset_tick;
    uint16_t fault_txe,fault_rxe;
    uint8_t fault_usbe,fault_events,fault_power;
    uint32_t session_epoch;
    uint16_t control_lines;
    /* JZ4730 has an unrelated dedicated FIFO UDC. Keep the shared prefix
     * stable and save its 32-bit registers/state separately. */
    uint32_t soc_saved[36],soc_sba,soc_tx_pending,soc_control_busy,soc_control_zlp;
    uint32_t soc_rx_length;
    uint8_t soc_rx[64];
#if RD_SOC_JZ4730
    uint32_t pullup_data,pullup_dir,pullup_func,pullup_irq;
#endif
} debug_usb;

/* 0=owned, 1=unsupported firmware, 2=external power or native USB session.
 * PB18 (4740) / GP89 (4730) external-power input follows the profile; its
 * physical relation to USB VBUS still needs hardware confirmation. The
 * caller starts unplugged; never take over an active mass-storage session. */
int debug_usb_open(debug_usb *u);
void debug_usb_poll(debug_usb *u);
/* read(): 0=no packet, -1=packet exceeds caller capacity, otherwise bytes.
 * write(): 1=packet queued (including zero length), 0=not writable.
 * A complete RPC whose length is a multiple of 64 needs an explicit ZLP. */
int debug_usb_read(debug_usb *u,uint8_t *data,unsigned capacity);
int debug_usb_write(debug_usb *u,const uint8_t *data,unsigned length);
int debug_usb_tx_idle(debug_usb *u);
int debug_usb_cable_connected(void);
unsigned debug_usb_frame(void);

#define DEBUG_USB_SNAPSHOT_WORDS 24u
#define DEBUG_USB_DIAG_WORDS 56u
/* Current and retained fault snapshots. JZ4740 uses the H1 diagnostic v1
 * register layout; JZ4730 v3 substitutes UDCFGR/UDIMR/UIMR/SBA/CFG and
 * software TX state at snapshot words12..18. Reconnect word7 is reserved0.
 * Snapshot reads avoid interrupt status registers with acknowledgement side
 * effects. No automatic software reconnect is implemented in this version. */
void debug_usb_diagnostics(const debug_usb *u,uint32_t out[DEBUG_USB_DIAG_WORDS]);
void debug_usb_close(debug_usb *u);
#endif
