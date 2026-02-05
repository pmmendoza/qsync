# Contributing to qsync

Thank you for your interest in contributing to qsync! This document provides guidelines and information for contributors.

## Getting Started

### Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/pmmendoza/qsync.git
   cd qsync
   ```

2. **Create a virtual environment:**
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install in editable mode with dev dependencies:**
   ```bash
   pip install -e ".[completion,pdf,langcheck]"
   pip install pytest pytest-cov ruff mypy
   ```

4. **Verify installation:**
   ```bash
   qsync --version
   qsync doctor
   ```

### Project Structure

```text
qsync/
├── src/qsync/          # Main package source
│   ├── cli.py          # CLI entry point
│   ├── config.py       # Configuration handling
│   └── ...             # Other modules
├── tests/              # Test suite
├── docs/               # Documentation
├── dev/                # Development notes and plans
└── pyproject.toml      # Project configuration
```

## How to Contribute

### Reporting Bugs

Before reporting a bug:
1. Check existing [GitHub Issues](https://github.com/pmmendoza/qsync/issues) to avoid duplicates
2. Run `qsync doctor --json` and include the output
3. Include the qsync version (`qsync --version`)

When reporting:
- Describe what you expected vs. what happened
- Include minimal steps to reproduce
- Include relevant error messages and logs

### Suggesting Features

Open a GitHub Issue with:
- A clear description of the feature
- Use cases explaining why this would be valuable
- Any relevant examples or mockups

### Submitting Changes

1. **Fork and branch:**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes:**
   - Follow the existing code style
   - Add tests for new functionality
   - Update documentation as needed

3. **Run checks:**
   ```bash
   # Format and lint
   ruff check src/
   ruff format src/

   # Type checking
   mypy src/qsync/

   # Tests
   pytest tests/
   ```

4. **Commit with clear messages:**
   ```bash
   git commit -m "Add feature: brief description"
   ```

5. **Submit a pull request:**
   - Reference any related issues
   - Describe what changed and why
   - Ensure CI checks pass

## Code Guidelines

### Style

- Follow [PEP 8](https://peps.python.org/pep-0008/) conventions
- Line length: 88 characters (Black/Ruff default)
- Use type hints for function signatures
- Use docstrings for public functions and classes

### Testing

- Write tests for new features and bug fixes
- Place tests in `tests/` mirroring the source structure
- Use descriptive test function names (`test_<function>_<scenario>`)

### Commit Messages

- Use imperative mood ("Add feature" not "Added feature")
- Keep the first line under 72 characters
- Reference issues when applicable (`Fixes #123`)

## Development Workflows

### Running Tests

```bash
# All tests
pytest tests/

# With coverage
pytest tests/ --cov=qsync --cov-report=term-missing

# Specific test file
pytest tests/test_config.py
```

### Code Quality

```bash
# Lint
ruff check src/

# Format
ruff format src/

# Type check
mypy src/qsync/
```

### Documentation

- Update `README.md` for user-facing changes
- Update `docs/` for detailed documentation
- Update `CHANGELOG.md` for notable changes

## Questions?

- Open a [GitHub Issue](https://github.com/pmmendoza/qsync/issues) for questions
- See [docs/troubleshooting.md](docs/troubleshooting.md) for common issues

## License

By contributing to qsync, you agree that your contributions will be licensed under the [GPL-3.0 License](LICENSE).
