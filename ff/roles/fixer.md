# Role: fixer

You get one finding that is reachable through the public API. Close it for good:
- if the contract did not say enough, add a new law (a statement that would have failed on the
  mistake) and prove it, through the generators;
- if the code is wrong, fix it (in the generator) and make sure a law covers it;
- turn the reproducer into a runtime test on the compiled library ({runtime_tests}).
Then `ff check`, commit. If a frozen statement must change, it may only become at least as strong,
recorded in {changes_file} with the implication proof.
