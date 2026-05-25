# Why Parquet for traces, not SQLite blobs

**Thesis:** The core access pattern is *column slicing by distance range* — "give
me the brake channel between 200 m and 400 m on lap 7". Row-oriented SQLite blobs
force you to read and deserialise the whole lap to get one channel; columnar
Parquet reads just that column's chunk.

To capture while building:
- [ ] Bytes per lap: SQLite-blob approach vs Parquet (zstd) — measured.
- [ ] Read latency for a single-channel distance slice, both approaches.
- [ ] The deciding point: traces are append-once, read-many, and always sliced by
      column + distance. That is the textbook columnar case.
