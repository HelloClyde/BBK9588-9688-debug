/* SPDX-License-Identifier: GPL-3.0-only */
#include "module_abi.h"
int debug9688_entry(const debug9688_api *api,debug9688_call *call)
{
    uint32_t sum=0;
    for(uint32_t i=0;i<call->args_size;i++){
        sum+=call->args[i];
        if((i&31u)==0 && api->poll_cancel())return -1;
    }
    if(call->output_capacity<4)return -2;
    for(unsigned i=0;i<4;i++)call->output[i]=(uint8_t)(sum>>(8*i));
    call->output_size=4;return (int)sum;
}
