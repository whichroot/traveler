CC = clang
CFLAGS = -O2 -Wall -Wextra -std=c99
LLC = /opt/homebrew/opt/llvm@21/bin/llc

tvc: tvc.c
	$(CC) $(CFLAGS) -o tvc tvc.c

test: tvc
	./tvc ../examples/field_basics.tv -o /tmp/field_basics.ll
	$(LLC) -filetype=obj /tmp/field_basics.ll -o /tmp/field_basics.o
	$(CC) /tmp/field_basics.o -o /tmp/field_basics
	@echo "--- running field_basics ---"
	/tmp/field_basics

clean:
	rm -f tvc

.PHONY: test clean
