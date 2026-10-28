PR validation case and image selection
======================================

The Azure DevOps PR pipeline uses ``.github/scripts/ai_select_cases.py`` to
produce ``ai_selection/ai_selected_cases.json``. This plan, rather than the
legacy YAML selection fragment, is the authoritative input to execution.

Selection rules
---------------

* Changed test methods, including metadata edits, are required. Shared helper
  edits conservatively include cases from the affected file or testsuite area.
  Changes in multiple files are accumulated.
* Framework feature and tool changes require at least one matching test for
  every detected feature/tool. AI ranks related candidates; it cannot remove
  these obligations. Priority and naming conventions are not evidence that a
  different test covers the same path.
* OS implementation changes follow public OS wrappers and in-file test helpers
  to find direct test callers. For example, changing ``Suse._update_packages``
  recalls tests that call ``node.os.update_packages``, rather than treating the
  OS change as an unrelated smoke-test request.
* The selector imports only the selected testsuite modules and checks their
  registered OS and platform requirements. Ambiguous method names are errors.
* Images come exclusively from the reviewed list in
  ``.github/copilot-instructions.md``. Changed OS classes and enclosing OS
  branches constrain image selection. For example, a SUSE-specific change cannot
  fall back to Ubuntu. Compatible cases are grouped by image.
* Version comparisons or changes affecting both architectures/generations need
  explicit images. Set the pipeline's ``validationImages`` parameter to a JSON
  array of exact strings from the reviewed list, for example:

  .. code-block:: json

     ["suse sles-12-sp5 gen2 latest", "suse sles-15-sp6 gen2 latest"]

  Explicit images are checked against the affected OS constraints. Reviewers
  must ensure these images cover the relevant version boundaries; the selector
  does not attempt to prove arbitrary conditional expressions.
* Documentation/comment-only changes produce ``not_required`` with no guest
  tests. Missing mappings, unsupported target distributions/platforms, invalid
  model responses with no covered obligations, and model API failures are not
  replaced with a successful-looking smoke run.

Model configuration
-------------------

Selection uses the Azure OpenAI v1 Responses API. Pipeline variables continue
to supply the API key, endpoint, and deployment name:

* ``azure_openai_api_key``: the existing secret API key.
* ``azure_openai_endpoint``: either the HTTPS resource root or its
  ``/openai/v1`` base URL, optionally with a trailing slash.
* ``azure_openai_deployment``: the actual Azure deployment name, for example
  ``gpt-5.4`` when a deployment with that name exists.

No ``azure_openai_api_version`` variable is needed. The request posts to
``/openai/v1/responses`` with the deployment in the ``model`` field. Authentication
remains API-key based; no Entra ID or service-connection change is required.
Reasoning-only, refused, incomplete, and empty responses are explicit errors,
not empty successful selections. The client does not send a ``temperature``
parameter, so deployments that do not support it can be used.

Execution and results
---------------------

``run_selected_cases.py`` runs inside the Azure CLI service-connection task.
Before deploying guests, it queries every selected image in the configured
region using ``az vm image show`` and resolves ``latest`` to a concrete version.
An unavailable target fails validation rather than switching distributions.

Groups run sequentially with exact method-name filters. The plan records
suite-qualified identities and rejects ambiguous method names. LISA retains
responsibility for VM capability/SKU matching; the selector's preflight checks
OS and platform, not every hardware or runtime prerequisite. Both stable and
preview cases can be selected so directly changed preview tests are not silently
excluded.

Each group has a fresh runtime log directory, passed to LISA as an absolute
``--log_path`` so execution and JUnit verification use the same directory.
Relative log paths would be prefixed with LISA's own runtime directory.
Every planned case must have
successful JUnit results: missing results, SKIPPED, ATTEMPTED, EXPUNGED, and failed
cases make validation incomplete or failed. Other groups still run after a
group fails. The ``pr_validation_results.json`` artifact records resolved image
versions and per-group status; runtime artifacts retain LISA environment logs.

Limitations
-----------

Static impact detection is conservative, not a coverage proof. In particular,
dynamic dispatch, indirect OS-specific helpers, runtime skips, hardware
requirements, and arbitrary version predicates still need reviewer attention.
Historical failure/flakiness data is not used as a selection input. A passing
plan demonstrates the selected scenarios, not every affected code path.
