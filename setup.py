from setuptools import setup, find_packages

packages = ["RevenueChaser"] + ["RevenueChaser." + p for p in find_packages(where=".")]

setup(
    name="RevenueChaser",
    version="0.1.0",
    description="Event-driven earnings decision engine",
    packages=packages,
    package_dir={"RevenueChaser": "."},
    python_requires=">=3.11",
)
