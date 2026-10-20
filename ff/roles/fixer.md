# Role: fixer

You get one finding that is reachable through the public API. Close it for good:
- if the contract did not say enough, add a new law (a statement that would have failed on the
  mistake) and prove it, {change_path};
- if the code is wrong, fix it {change_path} and make sure a law covers it;
- turn the reproducer into a runtime test on the compiled library ({runtime_tests}).
Then `ff check`, commit. Never weaken a law, or the specification, to make the finding go away.
