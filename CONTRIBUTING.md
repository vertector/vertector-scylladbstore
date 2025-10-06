# Contributing to Vertector ScyllaDB Store

Thank you for your interest in contributing to Vertector ScyllaDB Store! This document provides guidelines for contributing to the project.

## Development Setup

### Prerequisites

- Python 3.12 or 3.13
- Docker and Docker Compose (for local development)
- Git

### Local Development Environment

1. **Clone the repository**
   ```bash
   git clone https://github.com/vertector/vertector-scylladbstore.git
   cd vertector-scylladbstore
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install development dependencies**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Start local services**
   ```bash
   docker-compose up -d
   ```

5. **Verify setup**
   ```bash
   pytest tests/ -v
   ```

## Development Workflow

### Branch Strategy

- `main` - Production-ready code
- `develop` - Integration branch for features
- `feature/*` - New features
- `bugfix/*` - Bug fixes
- `hotfix/*` - Urgent production fixes

### Making Changes

1. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Write code following our style guide
   - Add tests for new functionality
   - Update documentation as needed

3. **Run tests locally**
   ```bash
   # Run all tests
   pytest

   # Run with coverage
   pytest --cov=src --cov-report=term

   # Run specific test file
   pytest tests/test_store.py -v
   ```

4. **Commit your changes**
   ```bash
   git add .
   git commit -m "feat: add new feature description"
   ```

   Follow [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat:` - New feature
   - `fix:` - Bug fix
   - `docs:` - Documentation changes
   - `test:` - Adding or updating tests
   - `refactor:` - Code refactoring
   - `perf:` - Performance improvements
   - `chore:` - Maintenance tasks

5. **Push to GitHub**
   ```bash
   git push origin feature/your-feature-name
   ```

6. **Create Pull Request**
   - Go to GitHub and create a PR
   - Fill out the PR template
   - Request review from team members

## Code Style

### Python Style Guide

We follow [PEP 8](https://pep8.org/) with some modifications:

- **Line length**: 100 characters (not 80)
- **Type hints**: Required for all public functions
- **Docstrings**: Google-style for all public APIs

### Code Formatting

```bash
# Format code with black
black src/ tests/

# Sort imports with isort
isort src/ tests/

# Lint with ruff
ruff check src/ tests/
```

### Type Checking

```bash
# Run mypy
mypy src/
```

## Testing Guidelines

### Test Structure

```python
class TestFeatureName:
    """Test suite for FeatureName."""

    @pytest.mark.asyncio
    async def test_specific_behavior(self, scylla_session, mock_embeddings):
        """Test that specific behavior works correctly."""
        # Arrange
        store = AsyncScyllaDBStore(...)

        # Act
        result = await store.some_operation()

        # Assert
        assert result == expected_value
```

### Coverage Requirements

- **Minimum coverage**: 50% overall
- **New code**: Aim for 80%+ coverage
- **Critical paths**: 100% coverage required

### Running Tests

```bash
# Run all tests
pytest

# Run specific test class
pytest tests/test_store.py::TestPutOperations

# Run with markers
pytest -m unit           # Fast unit tests only
pytest -m integration    # Integration tests
pytest -m "not load"     # Skip slow tests

# Run with coverage
pytest --cov=src --cov-report=html
open htmlcov/index.html  # View coverage report
```

## Documentation

### Docstring Format

Use Google-style docstrings:

```python
async def aput(
    self,
    namespace: tuple[str, ...],
    key: str,
    value: dict[str, Any],
    *,
    ttl: int | None = None
) -> None:
    """
    Store a document in the specified namespace.

    Args:
        namespace: Hierarchical namespace tuple (e.g., ("app", "users"))
        key: Unique key within the namespace
        value: Document data as a dictionary
        ttl: Optional time-to-live in seconds (0 = no expiration)

    Raises:
        StoreValidationError: If inputs are invalid
        StoreQueryError: If database operation fails

    Example:
        >>> await store.aput(("users",), "user123", {"name": "Alice"})
    """
```

### Updating Documentation

- Update README.md for user-facing changes
- Update CHANGELOG.md following [Keep a Changelog](https://keepachangelog.com/)
- Add examples for new features
- Update type hints and docstrings

## Pull Request Process

### PR Checklist

Before submitting a PR, ensure:

- [ ] All tests pass (`pytest`)
- [ ] Coverage hasn't decreased significantly
- [ ] Code is formatted (`black`, `isort`)
- [ ] Type hints are added (`mypy` passes)
- [ ] Documentation is updated
- [ ] CHANGELOG.md is updated
- [ ] Commit messages follow conventions

### PR Review Process

1. **Automated Checks**
   - CI/CD pipeline runs tests
   - Coverage report generated
   - Linting and type checking

2. **Code Review**
   - At least one team member approval required
   - Address all review comments
   - Resolve merge conflicts

3. **Merge**
   - Squash and merge to keep history clean
   - Delete feature branch after merge

## Performance Guidelines

### Benchmarking

Before submitting performance improvements:

```python
# Run performance tests
pytest tests/test_performance.py -v

# Profile specific operations
python -m cProfile -o profile.stats scripts/benchmark.py
```

### Performance Standards

- **Throughput**: Maintain 60+ docs/second
- **Latency**:
  - p50 < 20ms
  - p95 < 100ms
  - p99 < 200ms
- **Memory**: < 500MB for 10,000 documents

## Security

### Reporting Vulnerabilities

- **Do not** open public issues for security vulnerabilities
- Email: security@vertector.com
- Include detailed description and reproduction steps

### Security Checklist

- [ ] No hardcoded credentials
- [ ] Input validation on all user data
- [ ] SQL injection prevention
- [ ] Secrets in environment variables
- [ ] Dependencies regularly updated

## Release Process

### Version Numbering

We follow [Semantic Versioning](https://semver.org/):

- **Major** (1.0.0): Breaking changes
- **Minor** (1.1.0): New features (backward compatible)
- **Patch** (1.1.1): Bug fixes

### Creating a Release

1. **Update version** in `pyproject.toml`
2. **Update CHANGELOG.md** with release notes
3. **Create release branch**
   ```bash
   git checkout -b release/v1.1.0
   ```
4. **Tag the release**
   ```bash
   git tag -a v1.1.0 -m "Release v1.1.0"
   git push origin v1.1.0
   ```
5. **GitHub Actions** automatically:
   - Runs tests
   - Builds package
   - Publishes to PyPI
   - Creates GitHub release

## Getting Help

- **Documentation**: See README.md and docs/
- **Slack**: #vertector-scylladb-store
- **Email**: dev-team@vertector.com
- **Issues**: https://github.com/vertector/vertector-scylladbstore/issues

## Code of Conduct

### Our Pledge

We are committed to providing a welcoming and inclusive environment for all contributors.

### Expected Behavior

- Be respectful and professional
- Accept constructive criticism gracefully
- Focus on what's best for the project
- Show empathy towards other contributors

### Unacceptable Behavior

- Harassment or discriminatory comments
- Trolling or insulting remarks
- Publishing others' private information
- Other unprofessional conduct

## License

By contributing to Vertector ScyllaDB Store, you agree that your contributions will be licensed under the MIT License.

---

**Thank you for contributing to Vertector ScyllaDB Store!** 🎉
