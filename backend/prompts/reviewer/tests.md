Review type: test review. Review only the tests. Check that every behaviour the change adds or
changes has a test, that the tests would fail if the code were wrong (they assert real
results, not just that nothing crashed), that no test was skipped, deleted or weakened to make
it pass, and that edge cases and error paths are covered where they matter.
Ask for changes when changed behaviour has no meaningful test, or a test was weakened.
