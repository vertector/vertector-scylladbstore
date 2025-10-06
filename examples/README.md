# Examples

This directory contains example notebooks and scripts demonstrating how to use Vertector ScyllaDB Store.

## Notebooks

### 1. `semantic_search_demo.ipynb`
Demonstrates semantic search capabilities with vector embeddings.

**Topics covered:**
- Setting up embeddings with Google Gemini
- Storing documents with automatic embedding
- Performing semantic search
- Filtering results

**Requirements:**
- Google API key for embeddings
- ScyllaDB and Qdrant running

### 2. `test_crud_ops.ipynb`
Interactive examples of CRUD operations.

**Topics covered:**
- Creating and updating documents
- Reading documents with different filters
- Deleting documents
- Batch operations
- TTL (time-to-live) features

### 3. `test_langgraph_integration.ipynb`
Integration with LangGraph framework.

**Topics covered:**
- Using store with LangGraph agents
- State persistence
- Multi-agent coordination
- Memory management

## Running the Examples

### Prerequisites

1. **Start Services**:
   ```bash
   docker-compose up -d
   ```

2. **Install Dependencies**:
   ```bash
   pip install vertector-scylladbstore[langgraph]
   pip install jupyter notebook
   ```

3. **Set Environment Variables**:
   ```bash
   export GOOGLE_API_KEY="your-api-key"
   ```

### Launch Jupyter

```bash
jupyter notebook examples/
```

## Additional Scripts

See the `scripts/` directory for:
- `performance_benchmark.py` - Performance testing
- `performance_profiler.py` - Profiling and optimization
- `stress_test_qdrant.py` - Load testing

## Support

For questions or issues:
- GitHub Issues: https://github.com/vertector/vertector-scylladbstore/issues
- Documentation: https://github.com/vertector/vertector-scylladbstore/blob/main/README.md
