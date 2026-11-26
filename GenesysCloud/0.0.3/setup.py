from pathlib import Path

from setuptools import find_packages, setup


class VersionNotFoundError(Exception):
    """Raised when extension.yaml does not contain a version field."""


def find_version() -> str:
    extension_yaml_path = Path(__file__).parent / "extension" / "extension.yaml"
    with open(extension_yaml_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("version"):
                return line.split(" ", maxsplit=1)[-1].strip().strip('"')
    raise VersionNotFoundError(f"No version field found in {extension_yaml_path}")


setup(
    name="genesyscloud",
    version=find_version(),
    description="Genesyscloud python EF2 extension",
    author="Dynatrace",
    packages=find_packages(),
    python_requires=">=3.10",
    include_package_data=True,
    install_requires=["dt-extensions-sdk", "requests"],
    extras_require={"dev": ["dt-extensions-sdk[cli]"]},
)
