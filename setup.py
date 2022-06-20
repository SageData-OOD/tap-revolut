#!/usr/bin/env python
from setuptools import setup

setup(
    name="tap-revolut",
    version="0.1.0",
    description="Singer.io tap for extracting data",
    author="Stitch",
    url="http://singer.io",
    classifiers=["Programming Language :: Python :: 3 :: Only"],
    py_modules=["tap_revolut"],
    install_requires=[
        # NB: Pin these to a more specific version for tap reliability
        "singer-python==5.12.2",
        "requests",
    ],
    entry_points="""
    [console_scripts]
    tap-revolut=tap_revolut:main
    """,
    packages=["tap_revolut"],
    package_data={
        "schemas": ["tap_revolut/schemas/*.json"]
    },
    include_package_data=True,
)
