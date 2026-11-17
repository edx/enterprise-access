"""
The portable core of Personalized Search: the turn pipeline, its steps and the stream encoding.

Pure Python, standard library only. Nothing here imports Django, DRF, an HTTP client, a model SDK,
Algolia or any other enterprise-access app. Catalog search, model calls and session storage reach
the core through the ports in ``ports.py``; ``adapters/`` holds the implementations and ``wiring.py``
(the composition root) builds them from settings. ``tests/test_core_imports.py`` enforces this.
"""
