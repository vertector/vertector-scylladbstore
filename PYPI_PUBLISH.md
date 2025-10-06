# Publishing to PyPI

This document contains instructions for publishing `vertector-scylladbstore` to PyPI.

## Package Information

- **Package Name**: `vertector-scylladbstore`
- **Version**: 1.0.0
- **License**: MIT
- **Python**: >=3.12

## Built Artifacts

The following distribution files have been created and verified:

```
dist/
├── vertector_scylladbstore-1.0.0-py3-none-any.whl  (56KB)
└── vertector_scylladbstore-1.0.0.tar.gz            (86KB)
```

Both packages have been verified with `twine check` and **PASSED**.

## Pre-Publishing Checklist

- [x] All tests passing (52/52 tests ✅)
- [x] Package metadata complete in `pyproject.toml`
- [x] LICENSE file included (MIT)
- [x] README.md with installation and usage instructions
- [x] CHANGELOG.md with release notes
- [x] Distribution packages built successfully
- [x] Packages verified with `twine check`
- [ ] Test installation from built wheel
- [ ] Publish to TestPyPI first
- [ ] Verify installation from TestPyPI
- [ ] Publish to production PyPI

## Testing Installation Locally

Before publishing, test the built package:

```bash
# Create a fresh virtual environment
python -m venv test-env
source test-env/bin/activate  # or test-env\Scripts\activate on Windows

# Install from built wheel
pip install dist/vertector_scylladbstore-1.0.0-py3-none-any.whl

# Test basic import
python -c "from vertector_scylladbstore import AsyncScyllaDBStore; print('Import successful!')"

# Deactivate and clean up
deactivate
rm -rf test-env
```

## Publishing to TestPyPI (Recommended First Step)

TestPyPI is a separate instance of PyPI for testing package uploads without affecting the production index.

```bash
# Upload to TestPyPI
twine upload --repository testpypi dist/*

# You'll be prompted for:
# - Username: __token__
# - Password: Your TestPyPI API token (starts with pypi-)
```

### Test Installation from TestPyPI

```bash
# Install from TestPyPI
pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple \
    vertector-scylladbstore

# The --extra-index-url allows installing dependencies from production PyPI
```

## Publishing to Production PyPI

Once verified on TestPyPI:

```bash
# Upload to production PyPI
twine upload dist/*

# You'll be prompted for:
# - Username: __token__
# - Password: Your PyPI API token (starts with pypi-)
```

## After Publishing

1. **Verify on PyPI**:
   - Visit: https://pypi.org/project/vertector-scylladbstore/
   - Check that README renders correctly
   - Verify links work
   - Check classifiers are correct

2. **Test Installation**:
   ```bash
   pip install vertector-scylladbstore
   ```

3. **Create Git Tag**:
   ```bash
   git tag -a v1.0.0 -m "Release version 1.0.0"
   git push origin v1.0.0
   ```

4. **Create GitHub Release**:
   - Go to GitHub repository
   - Create new release from tag v1.0.0
   - Copy CHANGELOG.md content to release notes
   - Attach dist files (optional)

## API Tokens

To create PyPI API tokens:

1. **PyPI**:
   - Go to https://pypi.org/manage/account/
   - Navigate to "API tokens" section
   - Click "Add API token"
   - Name: "vertector-scylladbstore-upload"
   - Scope: "Project: vertector-scylladbstore" (after first upload)

2. **TestPyPI**:
   - Go to https://test.pypi.org/manage/account/
   - Follow same steps as PyPI

## Future Releases

For version updates:

1. Update version in `pyproject.toml` and `setup.py`
2. Update `CHANGELOG.md` with new changes
3. Run tests: `pytest`
4. Clean previous builds: `rm -rf dist/ build/ src/*.egg-info`
5. Build new packages: `python -m build`
6. Verify: `twine check dist/*`
7. Upload to TestPyPI first, then PyPI

## Security Notes

- **Never commit** API tokens to version control
- Use environment variables or `.pypirc` for credentials
- Consider using GitHub Actions for automated releases
- Enable 2FA on PyPI account

## Troubleshooting

### Common Issues

1. **Package name already exists**:
   - Choose a different name in `pyproject.toml`
   - Check https://pypi.org/ for availability

2. **Version already exists**:
   - Update version number in `pyproject.toml`
   - Rebuild package

3. **README not rendering**:
   - Validate Markdown syntax
   - Check relative links are correct
   - Consider using `python -m readme_renderer README.md` to test

4. **Missing dependencies**:
   - Verify all dependencies listed in `pyproject.toml`
   - Check version constraints are correct

## Support

For issues with publishing:
- PyPI Help: https://pypi.org/help/
- Packaging Guide: https://packaging.python.org/
- Twine Documentation: https://twine.readthedocs.io/
