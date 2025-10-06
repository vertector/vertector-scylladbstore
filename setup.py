"""Setup configuration for Vertector ScyllaDB Store."""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="vertector-scylladbstore",
    version="1.0.0",
    description="Production-ready ScyllaDB store with vector search capabilities via Qdrant",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Vertector Team",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.12",
    install_requires=[
        "scylla-driver==3.29.2",
        "qdrant-client>=1.15.1",
        "pydantic>=2.0.0",
        "python-dotenv",
        "tenacity>=9.1.2",
        "opentelemetry-api>=1.20.0",
        "opentelemetry-sdk>=1.20.0",
        "opentelemetry-instrumentation>=0.41b0",
        "prometheus-client>=0.18.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.4.0",
            "pytest-asyncio>=0.21.0",
            "pytest-cov>=4.1.0",
        ],
        "langgraph": [
            "langgraph",
            "langchain-google-genai",
        ],
    },
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Topic :: Database",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)
