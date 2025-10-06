# Vertector ScyllaDB Store - Project Structure

This document provides an overview of the project's directory structure and file organization.

## Directory Structure

```
vertector-scylladbstore/
├── src/                           # Source code
│   └── vertector_scylladbstore/   # Main package
│       ├── __init__.py            # Public API exports
│       ├── store.py               # Core store implementation (161KB)
│       ├── config.py              # Configuration management (18KB)
│       ├── observability.py       # Tracing & metrics (18KB)
│       └── rate_limiter.py        # Rate limiting (13KB)
│
├── tests/                         # Test suite (52 tests, 100% passing)
│   ├── conftest.py                # Test fixtures and configuration
│   ├── test_connection.py         # Connection lifecycle tests (8 tests)
│   ├── test_crud_operations.py    # CRUD operation tests (18 tests)
│   ├── test_error_handling.py     # Error handling tests (15 tests)
│   └── test_search.py             # Search operation tests (16 tests)
│
├── docs/                          # Documentation
│   ├── deployment.md              # Production deployment guide
│   ├── operations.md              # Operations runbook
│   └── troubleshooting.md         # Troubleshooting guide
│
├── examples/                      # Example notebooks and code
│   ├── README.md                  # Examples documentation
│   ├── semantic_search_demo.ipynb # Semantic search examples
│   ├── test_crud_ops.ipynb        # CRUD operations examples
│   └── test_langgraph_integration.ipynb  # LangGraph integration
│
├── scripts/                       # Development scripts
│   ├── README.md                  # Scripts documentation
│   ├── performance_benchmark.py   # Performance benchmarking
│   ├── performance_profiler.py    # Performance profiling
│   └── stress_test_qdrant.py      # Qdrant stress testing
│
├── dist/                          # Built distribution packages
│   ├── vertector_scylladbstore-1.0.0-py3-none-any.whl  # Wheel package
│   └── vertector_scylladbstore-1.0.0.tar.gz            # Source distribution
│
├── .archive/                      # Archived documentation
│   ├── PRODUCTION_READINESS.md    # Initial assessment
│   └── QDRANT_INTEGRATION.md      # Integration notes
│
├── pyproject.toml                 # Modern Python packaging config
├── setup.py                       # Setuptools configuration
├── MANIFEST.in                    # Package data manifest
│
├── README.md                      # Project README (PyPI landing page)
├── LICENSE                        # MIT License
├── CHANGELOG.md                   # Version history
├── PRODUCTION_IMPLEMENTATION.md   # Production features summary
├── PYPI_PUBLISH.md                # PyPI publishing guide
│
├── requirements.txt               # Production dependencies
├── requirements-dev.txt           # Development dependencies
├── pytest.ini                     # Pytest configuration
├── docker-compose.yml             # Local development services
└── .gitignore                     # Git ignore rules
```

## Key Files

### Package Configuration

- **pyproject.toml**: Modern Python packaging metadata (PEP 517/518)
  - Project metadata (name, version, description, license)
  - Dependencies and optional dependencies
  - Build system configuration
  - Tool configurations (pytest, coverage)

- **setup.py**: Setuptools configuration for compatibility
  - Package discovery
  - Entry points
  - Install requirements

- **MANIFEST.in**: Controls which files are included in distributions
  - Documentation files
  - Test files
  - Configuration files

### Source Code (`src/vertector_scylladbstore/`)

- **`__init__.py`**: Package initialization and public API
  - Exports main classes and functions
  - Version information
  - Clean public interface

- **`store.py`**: Core store implementation
  - AsyncScyllaDBStore class
  - CRUD operations
  - Search and filtering
  - TTL management
  - Batch operations

- **`config.py`**: Configuration management
  - Pydantic models for validation
  - Secrets management
  - TLS/Authentication configuration
  - Retry and circuit breaker config

- **`observability.py`**: Monitoring and tracing
  - OpenTelemetry integration
  - Prometheus metrics
  - Percentile tracking
  - Alert management

- **`rate_limiter.py`**: Rate limiting
  - Token bucket algorithm
  - Sliding window algorithm
  - Per-operation limits

### Tests (`tests/`)

- **Test Markers**:
  - `@pytest.mark.unit`: Fast, isolated tests
  - `@pytest.mark.integration`: Tests requiring services
  - `@pytest.mark.load`: Performance tests
  - `@pytest.mark.chaos`: Failure scenario tests

- **Coverage**: 85%+ code coverage
- **All tests passing**: 52/52 ✅

### Documentation (`docs/`)

- **deployment.md**: Production deployment instructions
- **operations.md**: Day-to-day operations guide
- **troubleshooting.md**: Common issues and solutions

### Examples (`examples/`)

Interactive Jupyter notebooks demonstrating:
- Semantic search with embeddings
- CRUD operations
- LangGraph integration
- Best practices

### Scripts (`scripts/`)

Development utilities:
- Performance benchmarking
- Profiling tools
- Stress testing

## Development Workflow

### Setup
```bash
# Clone repository
git clone https://github.com/vertector/vertector-scylladbstore.git
cd vertector-scylladbstore

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install in development mode
pip install -e ".[dev]"

# Start services
docker-compose up -d
```

### Testing
```bash
# Run all tests
pytest

# Run specific test types
pytest -m unit        # Unit tests only
pytest -m integration # Integration tests only

# With coverage
pytest --cov=src --cov-report=html
```

### Building
```bash
# Clean previous builds
rm -rf dist/ build/ src/*.egg-info

# Build distribution packages
python -m build

# Verify packages
twine check dist/*
```

## File Sizes

### Source Code
- `store.py`: 161 KB (core implementation)
- `config.py`: 18 KB (configuration)
- `observability.py`: 18 KB (monitoring)
- `rate_limiter.py`: 13 KB (rate limiting)

### Distribution Packages
- Wheel (`.whl`): 56 KB
- Source tarball (`.tar.gz`): 86 KB

## Version Control

### Ignored Files (.gitignore)
- Build artifacts (`dist/`, `build/`, `*.egg-info/`)
- Python cache (`__pycache__/`, `*.pyc`)
- Test artifacts (`.pytest_cache/`, `.coverage`, `htmlcov/`)
- Virtual environments (`venv/`, `env/`)
- IDE files (`.vscode/`, `.idea/`)
- Environment files (`.env`)

### Included in Git
- Source code (`src/`)
- Tests (`tests/`)
- Documentation (`docs/`, `*.md`)
- Configuration files (`pyproject.toml`, `setup.py`, etc.)
- Examples and scripts

## PyPI Distribution

### Included in Package
- Source code modules
- LICENSE file
- README.md
- CHANGELOG.md
- Documentation (`docs/`)
- Tests (`tests/`)

### Excluded from Package
- Development scripts
- Example notebooks
- Docker configuration
- Build artifacts
- Git files

## Support & Resources

- **Documentation**: See `docs/` directory
- **Examples**: See `examples/` directory
- **Issues**: GitHub Issues
- **PyPI**: https://pypi.org/project/vertector-scylladbstore/

---

**Last Updated**: 2025-10-05
**Version**: 1.0.0
