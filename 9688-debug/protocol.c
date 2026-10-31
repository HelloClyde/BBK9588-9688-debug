/* SPDX-License-Identifier: GPL-3.0-only
 * Based on H1Debug experiments/h1-usb-debug/protocol.c; independent 96DB ID. */
#include "protocol.h"
uint32_t debug_crc32(const uint8_t *data,unsigned length)
{
    uint32_t c=~0u;
    for(unsigned i=0;i<length;i++){c^=data[i];for(unsigned j=0;j<8;j++)c=(c>>1)^(0xedb88320u&(0u-(c&1u)));}
    return ~c;
}
static unsigned get16(const uint8_t *p){return p[0]|((unsigned)p[1]<<8);}
static uint32_t get32(const uint8_t *p){return get16(p)|((uint32_t)get16(p+2)<<16);}
static void put16(uint8_t *p,unsigned n){p[0]=(uint8_t)n;p[1]=(uint8_t)(n>>8);}
void debug_protocol_feed(debug_protocol *p,const uint8_t *data,unsigned length,debug_command_fn command)
{
    for(unsigned i=0;i<length;i++){
        if(p->rx_count>=sizeof p->rx){p->rx_count=0;p->bad_frames++;}
        p->rx[p->rx_count++]=data[i];
        if(p->rx_count==4 && (p->rx[0]!='9' || p->rx[1]!='6' || p->rx[2]!='D' || p->rx[3]!='B')){
            for(unsigned j=0;j<3;j++)p->rx[j]=p->rx[j+1];p->rx_count=3;continue;
        }
        if(p->rx_count<12)continue;
        unsigned n=get16(p->rx+8),total=16+n;
        if(n>512 || p->rx[4]!=1){p->rx_count=0;p->bad_frames++;continue;}
        if(p->rx_count<total)continue;
        if(get32(p->rx+12+n)!=debug_crc32(p->rx,12+n) || get16(p->rx+10) || (p->rx[5]&128) || p->tx_size){
            p->rx_count=0;p->bad_frames++;continue;
        }
        unsigned status=0,used=command(p->rx[5],p->rx+12,n,p->tx+12,&status);
        if(used>512){used=0;status=1;}
        for(unsigned j=0;j<8;j++)p->tx[j]=p->rx[j];p->tx[5]|=128;
        put16(p->tx+8,used);put16(p->tx+10,status);
        uint32_t crc=debug_crc32(p->tx,12+used);
        for(unsigned j=0;j<4;j++)p->tx[12+used+j]=(uint8_t)(crc>>(8*j));
        p->tx_size=16+used;p->tx_offset=0;p->rx_count=0;
    }
}
