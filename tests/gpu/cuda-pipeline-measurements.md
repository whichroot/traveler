# CUDA staging and graph measurements

## Environment and method

Recorded 2026-09-23 on Jane: NVIDIA RTX PRO 6000 Blackwell Workstation Edition,
native SM120, driver 595.99.02, LLVM 21.1.8. Isolated checkout:
`/tmp/traveler-k4.Awntm1`. No GPU compute processes were listed before execution.
GPU clocks, power limits, CPU affinity, and system configuration were not changed.

The compiler snapshot is
`a4cb081fc74a15a619903935af7ab558b9ee25b43176dd7182149e57872032dc`.
The measurement program is `cuda_pipeline_gate.tv`; the runner is
`check_cuda_pipeline.py`. Reproduce through the command in `BUILD.md`.
The root-only `/dev/dax0.0` fixture used `--sudo-dax`; it opens the device read-only
and maps with read protection. The ordinary-memory fixture runs as the normal user.

Each sample processes 32 chunks. Each chunk stages source bytes into pinned memory,
uploads, doubles each u64 in an independent kernel (wrapping arithmetic), and
downloads into pinned output. Two fixed source windows and two reusable slots are
used. DAX mapping, initial source snapshot, allocation, package loading/JIT, graph
construction, and warm-up precede the timed samples. This measures reuse of an
already mapped source window, not a cold sequential scan of the entire DAX device.

Modes:

- **Serial:** wait for each chunk's completion before continuing.
- **Pipeline:** alternate two streams/slots, waiting before each slot is reused.
- **Graph:** the same two-slot pipeline, with one captured upload/kernel/download
  graph replay replacing three direct enqueue calls for each chunk.

All modes use the same checked runtime and event completion. Timings are host
`CLOCK_MONOTONIC` wall time through final completion, including CPU staging,
submission, copies, kernel work, and waits. Copying final pinned outputs to ordinary
host memory and checking every output element occur after timing each sample.
These are not isolated kernel timings or hardware overlap traces.

There are seven samples per mode, with mode order rotated each trial. A 250 ms
graph-replay warm-up precedes each source/size measurement process. The reported
setup figure includes graph creation, initial launches, and that warm-up, but
excludes earlier module loading and allocation. It is not graph-instantiation cost
alone. Earlier runs with only one warm-up replay showed substantial drift; even
with longer warm-up, outliers remain. All seven samples are retained below.

## Results

Times are milliseconds per 32 chunks: **median [minimum, maximum]**.

| Source | Chunk | Serial | Pipeline | Graph |
| --- | ---: | ---: | ---: | ---: |
| Ordinary memory | 4 KiB | 0.767 [0.760, 0.823] | 0.570 [0.557, 0.590] | 0.562 [0.549, 0.593] |
| Ordinary memory | 1 MiB | 4.107 [4.091, 4.123] | 2.585 [2.576, 2.586] | 2.559 [2.550, 2.572] |
| Ordinary memory | 16 MiB | 61.820 [61.781, 66.019] | 45.622 [45.525, 46.041] | 45.528 [45.472, 45.647] |
| DAX | 4 KiB | 0.444 [0.431, 0.459] | 0.300 [0.296, 0.308] | 0.290 [0.285, 0.301] |
| DAX | 1 MiB | 4.040 [4.010, 5.830] | 2.548 [2.540, 3.837] | 2.562 [2.552, 3.820] |
| DAX | 16 MiB | 76.691 [76.564, 100.227] | 58.479 [58.147, 62.602] | 58.406 [58.185, 59.621] |

Pipelining reduces the median wall time in every measured workload. Graph replay's
additional change is small and is not consistently positive: the DAX 1 MiB median
is slightly higher with graphs. These data support pipelining for this workload;
they do not establish a general graph speedup or prove simultaneous copy/compute
execution. Cross-source timings are separate process runs and are not a controlled
DAX-versus-RAM latency comparison.

## Raw samples

All values below are nanoseconds, in collection order.

```text
ordinary 4096 setup=250511842
serial   822721 766673 763994 776384 786621 766699 759697
pipeline 580411 567609 557308 558890 569515 589973 583584
graph    562480 549451 592854 559965 557404 582850 579147

ordinary 1048576 setup=251342128
serial   4122769 4100007 4092980 4108033 4090946 4107625 4107089
pipeline 2577684 2584877 2576347 2586003 2585682 2585284 2583382
graph    2557157 2565621 2559189 2571791 2558800 2556493 2550000

ordinary 16777216 setup=256550085
serial   66018512 61798881 61820422 61860402 61814425 61867770 61780796
pipeline 46040881 45682406 45703743 45612408 45609532 45622070 45524634
graph    45647486 45557884 45542067 45472219 45500632 45481591 45528071

dax 4096 setup=250334097
serial   459269 455543 445015 434536 443801 431224 433203
pipeline 299348 299780 307597 296383 301254 298112 299752
graph    285046 290740 285843 290364 288603 301284 294433

dax 1048576 setup=251290654
serial   5830044 4531708 4029811 4040315 4010332 4041274 4028816
pipeline 3836642 3835723 2552863 2544005 2546221 2548154 2539945
graph    3819649 3808561 2562332 2559130 2560340 2561820 2552270

dax 16777216 setup=260011307
serial   100227421 77061046 76998639 76564080 76690527 76626922 76618936
pipeline 62602006 59022597 58423510 58164180 58479266 58147123 58521592
graph    59620942 58635921 58533183 58185497 58406324 58346684 58274038
```
