"""
OCI GenAI helpers.

Builds a chat LLM through the same path the main AIDP agent uses
(OCIAIConf + init_oci_llm), defaulting to resource-principal auth so no keys
are handled in the tool. Imports are done lazily inside the functions because
these modules live on the compute runtime, not the agent-service runtime.

A single client builder (``build_oci_genai_client``) is exposed for the
embeddings path so chat and embeddings share the same compartment / region /
auth resolution.
"""

from .config_utils import get_cfg, resolve_oci_conf


def build_llm(conf, *, max_tokens_override=None, temperature_override=None):
    from aidputils.agents.toolkit.configs import OCIAIConf
    from aidputils.agents.toolkit.agent_helper import init_oci_llm

    resolved = resolve_oci_conf(conf)
    max_tokens = (
        max_tokens_override
        if max_tokens_override is not None
        else get_cfg(conf, "max_tokens", 2000)
    )
    temperature = (
        temperature_override
        if temperature_override is not None
        else get_cfg(conf, "temperature", 0.0)
    )
    oci_conf = OCIAIConf(
        model_id=resolved["model_id"],
        model_provider=resolved["model_provider"],
        endpoint=resolved["endpoint"],
        compartment_id=resolved["compartment_id"],
        auth_type=resolved["auth_type"],
        auth_profile=resolved["auth_profile"],
        model_args={
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
    )
    return init_oci_llm(oci_conf)


def call_llm(conf, system_prompt, user_content, **overrides):
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_llm(conf, **overrides)
    messages = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    messages.append(HumanMessage(content=user_content))
    resp = llm.invoke(messages)
    return getattr(resp, "content", str(resp))


def build_oci_genai_client(conf):
    """Build a raw OCI GenAI inference client for the embeddings path.

    Auth resolution (in order):
      1. conf.credential_name set      -> aidputils.secrets bundle ->
                                          oci.signer.Signer(private_key_content=...)
      2. auth_type=resource_principal  -> resource principal (default)
      3. auth_type=instance_principal  -> instance principal
      4. else                          -> oci.config.from_file (profile-based)

    Returns (client, resolved_conf).
    """
    import oci

    resolved = resolve_oci_conf(conf)
    endpoint = resolved["endpoint"]

    # 1. Credential Store path.
    try:
        from .credential_resolver import resolve_oci_signer
        from .config_utils import get_cfg
        cred_name = get_cfg(conf, "credential_name", "")
        cs_signer, _meta, cred_err = resolve_oci_signer(cred_name)
        if cred_err:
            raise ValueError(f"credential_name='{cred_name}' failed: {cred_err}")
        if cs_signer is not None:
            client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                config={}, signer=cs_signer, service_endpoint=endpoint
            )
            return client, resolved
    except ImportError:
        pass  # helper not bundled — fall through to legacy paths

    # 2-4. Legacy paths.
    auth_type = (resolved.get("auth_type") or "resource_principal").lower()
    if auth_type == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.generative_ai_inference.GenerativeAiInferenceClient(
            config={}, signer=signer, service_endpoint=endpoint
        )
    elif auth_type == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.generative_ai_inference.GenerativeAiInferenceClient(
            config={}, signer=signer, service_endpoint=endpoint
        )
    else:
        oci_config = oci.config.from_file(profile_name=resolved["auth_profile"])
        client = oci.generative_ai_inference.GenerativeAiInferenceClient(
            config=oci_config, service_endpoint=endpoint
        )
    return client, resolved


def estimate_tokens(text):
    if not text:
        return 0
    return max(1, len(text) // 4)
