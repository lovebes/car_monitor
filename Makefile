all: gpio_poll hud

gpio_poll: gpio_poll.c argparse.c argparse.h
	gcc -g -Os -Wall gpio_poll.c argparse.c -o gpio_poll.bin && mv gpio_poll.bin gpio_poll

gpio_poll_sim: gpio_poll.c argparse.c argparse.h
	gcc -Os -DGPIO_SIM -Wall gpio_poll.c argparse.c -o gpio_poll_sim

hud: hud.c
	gcc -Os -Wall hud.c -g -o hud.n `pkg-config cairo --cflags --libs` && mv hud.n hud

hud-sim: hud.c
	gcc -Os -Wall hud.c -DSDL_SIM -g -o hud-sim `pkg-config cairo --cflags --libs` `sdl2-config --cflags --libs`


hud_noopt: hud.c
	gcc -O0 -Wall hud.c -g -o hud_noopt `pkg-config cairo --cflags --libs`

canlog_shmem: canlog_shmem.c
	gcc -Os -Wall canlog_shmem.c -g -o canlog_shmem.tmp -lz && mv canlog_shmem.tmp canlog_shmem
