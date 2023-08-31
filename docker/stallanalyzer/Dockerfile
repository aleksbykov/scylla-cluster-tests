FROM ubuntu:23.04
RUN apt update  \
	&& apt-get install -y --no-install-recommends build-essential cmake libssl-dev zlib1g-dev libffi-dev clang-format clang-tidy clang-tools clang clangd libc++-dev libc++1 libc++abi-dev libc++abi1 libclang-dev libclang1 liblldb-dev libllvm-ocaml-dev libomp-dev libomp5 lld lldb llvm-dev llvm-runtime llvm python3-clang binutils curl ca-certificates openssl file # buildkit
RUN curl -O https://raw.githubusercontent.com/scylladb/seastar/master/scripts/addr2line.py  \
	&& curl -O https://raw.githubusercontent.com/scylladb/seastar/master/scripts/stall-analyser.py # buildkit
RUN chmod +x /stall-analyser.py # buildkit
RUN ln -s /usr/bin/python3 /usr/bin/python # buildkit
ENTRYPOINT ["/stall-analyser.py"]
