@ Branch shapes the compiler may emit but that branchy.c cannot force:
@ cbz/cbnz, conditional return (bx lr / pop {pc}) and conditional call in IT.
    .syntax unified
    .thumb
    .text

    .global asm_cbz
    .type asm_cbz, %function
asm_cbz:                    @ r0 == 0 -> 1, else 2
    cbz r0, 1f
    movs r0, #2
    bx lr
1:  movs r0, #1
    bx lr

    .global asm_cbnz
    .type asm_cbnz, %function
asm_cbnz:                   @ r0 != 0 -> 1, else 0
    cbnz r0, 1f
    bx lr
1:  movs r0, #1
    bx lr

    .global asm_ret_eq
    .type asm_ret_eq, %function
asm_ret_eq:                 @ conditional return via IT + bx lr
    cmp r0, #0
    it eq
    bxeq lr
    adds r0, #10
    bx lr

    .global asm_pop_ne
    .type asm_pop_ne, %function
asm_pop_ne:                 @ conditional return via IT + pop {pc}
    push {r4, lr}
    movs r4, r0
    cmp r4, #5
    it ne
    popne {r4, pc}
    movs r0, #55
    pop {r4, pc}

    .global asm_call_gt
    .type asm_call_gt, %function
asm_call_gt:                @ conditional call: bl inside IT block
    push {r4, lr}
    cmp r0, #1
    it gt
    blgt asm_cbz
    pop {r4, pc}
