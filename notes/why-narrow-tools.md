# Why narrow tools beat one god-tool

**Thesis:** Claude routes far more reliably to 8 well-described, single-purpose
tools than to one `get_telemetry` mega-tool with a dozen modal parameters. The
tool description *is* the prompt engineering.

To capture while building:
- [ ] A concrete example of Claude doing the wrong thing with an over-broad tool
      (wrong params, or dumping a raw trace when a summary was wanted).
- [ ] Before/after tool descriptions and the routing improvement they bought.
- [ ] The "what to return" discipline: each tool returns one shape, pre-sized.
