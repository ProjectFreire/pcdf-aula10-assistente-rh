
"""
ETAPA 10.1 — Governança e observabilidade dos agentes.

Registra:
- Ferramentas chamadas;
- Argumentos enviados;
- Resultados resumidos;
- Tokens consumidos.

Os controles B.1, B.2 e B.3 são opcionais e
permanecem desativados nesta implementação.
"""

import json

from agents import AgentsException, RunHooks


class LimiteExcedido(AgentsException):
    """Exceção reservada para controles opcionais."""


# B.1 — Permissões opcionais.
PERMISSOES: dict[str, set[str]] = {}


class Governanca(RunHooks):

    def __init__(
        self,
        max_tools: int = 6,
        max_llm: int = 12,
        verbose: bool = True,
    ):
        self.max_tools = max_tools
        self.max_llm = max_llm
        self.verbose = verbose

        self.tools_chamadas = 0
        self.chamadas_llm = 0

        self.eventos: list[dict] = []

    async def on_tool_start(
        self,
        context,
        agent,
        tool,
    ) -> None:
        """Conta chamadas de ferramentas."""

        self.tools_chamadas += 1

        if self.verbose:
            print(
                f"[governança] {agent.name} "
                f"-> {tool.name}: início"
            )

    async def on_llm_start(
        self,
        context,
        agent,
        system_prompt,
        input_items,
    ) -> None:
        """Conta chamadas ao modelo."""

        self.chamadas_llm += 1

        if self.verbose:
            print(
                f"[governança] {agent.name}: "
                f"chamada LLM {self.chamadas_llm}"
            )

    async def on_tool_end(
        self,
        context,
        agent,
        tool,
        result,
    ) -> None:
        """
        ETAPA 10.1 — Registra uma ferramenta executada.

        Guarda:
        - agente
        - tool
        - argumentos
        - resultado truncado
        """

        argumentos = getattr(
            context,
            "tool_arguments",
            "",
        )

        if argumentos is None:
            argumentos = ""

        if not isinstance(argumentos, str):
            argumentos = json.dumps(
                argumentos,
                ensure_ascii=False,
                default=str,
            )

        resultado = str(result)

        # Evita armazenar respostas muito extensas.
        resultado = resultado[:500]

        evento = {
            "agente": agent.name,
            "tool": tool.name,
            "argumentos": argumentos,
            "resultado": resultado,
        }

        self.eventos.append(evento)

        if self.verbose:
            print(
                f"[governança] {agent.name} "
                f"-> {tool.name}: concluída"
            )

    async def on_agent_end(
        self,
        context,
        agent,
        output,
    ) -> None:
        """
        ETAPA 10.1 — Registra o consumo de tokens.

        Obtém os tokens do contexto de execução.
        """

        usage = getattr(
            context,
            "usage",
            None,
        )

        tokens = getattr(
            usage,
            "total_tokens",
            0,
        )

        if tokens is None:
            tokens = 0

        evento = {
            "agente": agent.name,
            "tokens": tokens,
        }

        self.eventos.append(evento)

        if self.verbose:
            print(
                f"[governança] {agent.name}: "
                f"{tokens} tokens"
            )
