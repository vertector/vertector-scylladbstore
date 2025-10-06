# Development Scripts

This directory contains development and testing scripts for Vertector ScyllaDB Store.

## Scripts

### `performance_benchmark.py`
Benchmark store performance under various workloads.

**Usage:**
```bash
python scripts/performance_benchmark.py
```

**Measures:**
- Throughput (docs/sec)
- Latency percentiles (p50, p95, p99)
- Cache hit rates
- Batch operation performance

### `performance_profiler.py`
Profile store operations to identify bottlenecks.

**Usage:**
```bash
python scripts/performance_profiler.py
```

**Outputs:**
- CPU profiling data
- Memory usage analysis
- Hotspot identification
- Optimization recommendations

### `stress_test_qdrant.py`
Stress test Qdrant vector search integration.

**Usage:**
```bash
python scripts/stress_test_qdrant.py
```

**Tests:**
- High concurrent write loads
- Large batch insertions
- Vector search under load
- Sync reliability

## Requirements

Install development dependencies:
```bash
pip install vertector-scylladbstore[dev]
```

## Running All Scripts

```bash
# Run benchmarks
python scripts/performance_benchmark.py

# Profile operations
python scripts/performance_profiler.py

# Stress test
python scripts/stress_test_qdrant.py
```

## Contributing

When adding new scripts:
1. Add them to this directory
2. Update this README
3. Include usage instructions
4. Document any special requirements
