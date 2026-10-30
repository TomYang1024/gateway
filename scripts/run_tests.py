import asyncio
import os

from test_setup import setup

asyncio.run(setup())
os.execvp(
    "pytest",
    [
        "pytest",
        "-q",
        "--cov=llm_gateway_core",
        "--cov=llm_gateway_services",
        "--cov=llm_gateway_infrastructure",
        "--cov=llm_gateway_api",
        "--cov=llm_gateway_sdk",
        "--cov-report=term",
        "--cov-report=xml:test-results/container-coverage.xml",
        "--junitxml=test-results/container-junit.xml",
    ],
)
