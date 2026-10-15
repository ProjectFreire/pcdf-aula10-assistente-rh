
"""
ETAPA 6 — Especialistas e Coordenador de RH.
"""

import json

from agents import (
    Agent,
    ModelSettings,
    RunHooks,
    ToolCallOutputItem,
    function_tool,
)

from etapa05_cliente_mcp import chamar_mcp_async
from etapa04_ferramentas import cotacao_usd_brl


CONFIG = ModelSettings(temperature=0, max_tokens=80)


@function_tool
async def consultar_normas(tema: str) -> str:
    """Consulta as normas oficiais de RH para o tema informado."""
    resultado = await chamar_mcp_async(
        "consultar_normas", {"tema": tema}
    )
    return json.dumps(resultado, ensure_ascii=False)


@function_tool
async def consultar_escala(
    equipe: str,
    inicio: str,
    fim: str,
) -> str:
    """Consulta os afastamentos da equipe no período."""
    resultado = await chamar_mcp_async(
        "consultar_escala",
        {"equipe": equipe, "inicio": inicio, "fim": fim},
    )
    return json.dumps(resultado, ensure_ascii=False)


@function_tool
async def tabela_diarias(tipo_destino: str) -> str:
    """Consulta a tabela oficial de diárias."""
    resultado = await chamar_mcp_async(
        "tabela_diarias",
        {"tipo_destino": tipo_destino},
    )
    return json.dumps(resultado, ensure_ascii=False)


@function_tool
async def consultar_remuneracao(matricula: str) -> str:
    """Consulta remuneração para cálculo financeiro restrito."""
    resultado = await chamar_mcp_async(
        "consultar_remuneracao",
        {"matricula": matricula},
    )
    return json.dumps(resultado, ensure_ascii=False)


@function_tool
def cotacao_dolar() -> str:
    """Consulta a cotação do dólar."""
    cotacao = cotacao_usd_brl()
    return f"{cotacao:.4f}" if cotacao else "indisponível"


async def extrair_resultados_ferramentas(resultado) -> str:
    """Recupera as saídas efetivas das ferramentas do especialista."""
    saidas = []

    for item in resultado.new_items:
        if isinstance(item, ToolCallOutputItem):
            conteudo = item.output

            if isinstance(conteudo, str):
                try:
                    conteudo = json.loads(conteudo)
                except json.JSONDecodeError:
                    pass

            saidas.append(conteudo)

    if saidas:
        return json.dumps(
            saidas, ensure_ascii=False, default=str
        )

    return json.dumps(
        {"erro": "Especialista não executou consulta verificável."},
        ensure_ascii=False,
    )


async def extrair_resultado_financeiro(resultado) -> str:
    """
    Não encaminha ao Coordenador o retorno bruto de remuneração.
    O salário-base é informação restrita ao Financeiro.
    """
    return json.dumps(
        {
            "observacao": (
                "Consulta financeira executada pelo especialista. "
                "Dados brutos de remuneração não são divulgados."
            ),
            "parecer": str(resultado.final_output),
        },
        ensure_ascii=False,
    )


def criar_normas() -> Agent:
    return Agent(
        name="Normas",
        instructions=(
            "Você é especialista em normas de RH. "
            "Consulte consultar_normas uma vez com o tema recebido. "
            "Explique brevemente as regras encontradas. "
            "Não invente leis ou restrições. "
            "Não registre decisões."
        ),
        tools=[consultar_normas],
        model_settings=CONFIG,
        model="llama3.1",
    )


def criar_escala() -> Agent:
    return Agent(
        name="Escala",
        instructions=(
            "Você é especialista em escala de RH. "
            "Consulte consultar_escala uma vez para a equipe "
            "e o período recebidos. "
            "Informe os conflitos encontrados. "
            "Se houver conflito, sugira verificar outro período "
            "sem afirmar disponibilidade não consultada. "
            "Não invente afastamentos. "
            "Não registre decisões."
        ),
        tools=[consultar_escala],
        model_settings=CONFIG,
        model="llama3.1",
    )


def criar_financeiro() -> Agent:
    return Agent(
        name="Financeiro",
        instructions=(
            "Você é especialista financeiro de RH. "
            "Para venda de férias, consulte consultar_remuneracao "
            "e calcule dias_vendidos * salario_base / 30. "
            "Para diárias, consulte tabela_diarias. "
            "Considere uma diária por pernoite e meia no retorno. "
            "Para exterior, consulte cotacao_dolar quando necessário. "
            "Não invente valores nem divulgue salário-base. "
            "Não registre decisões."
        ),
        tools=[
            tabela_diarias,
            consultar_remuneracao,
            cotacao_dolar,
        ],
        model_settings=CONFIG,
        model="llama3.1",
    )


def criar_redator() -> Agent:
    return Agent(
        name="Redator",
        instructions=(
            "Redija despacho administrativo curto e formal. "
            "Use somente os fatos verificados fornecidos. "
            "Respeite a decisão objetiva calculada pelo Python. "
            "Se depender da chefia, informe que está pendente "
            "e não declare deferimento definitivo. "
            "Se houver indeferimento, indique as violações. "
            "Não invente normas, valores, datas ou aprovações. "
            "Não inclua CPF, CID ou dados de saúde. "
            "Responda somente com o despacho."
        ),
        tools=[],
        model_settings=CONFIG,
        model="llama3.1",
    )


def criar_coordenador(
    hooks: RunHooks | None = None,
    tools_extras: list | None = None,
) -> Agent:

    ferramentas = [
        criar_normas().as_tool(
            tool_name="parecer_normas",
            tool_description="Consulta normas oficiais de RH.",
            custom_output_extractor=extrair_resultados_ferramentas,
            hooks=hooks,
            failure_error_function=None,
            max_turns=3,
        ),
        criar_escala().as_tool(
            tool_name="parecer_escala",
            tool_description="Consulta a escala da equipe.",
            custom_output_extractor=extrair_resultados_ferramentas,
            hooks=hooks,
            failure_error_function=None,
            max_turns=3,
        ),
        criar_financeiro().as_tool(
            tool_name="parecer_financeiro",
            tool_description=(
                "Consulta informações financeiras aplicáveis "
                "sem divulgar remuneração restrita."
            ),
            custom_output_extractor=extrair_resultado_financeiro,
            hooks=hooks,
            failure_error_function=None,
            max_turns=4,
        ),
        *(tools_extras or []),
    ]

    return Agent(
        name="Coordenador",
        instructions=(
            "Você coordena especialistas de RH. "
            "Consulte parecer_normas para as regras aplicáveis. "
            "Consulte parecer_escala para férias ou abono. "
            "Consulte parecer_financeiro apenas para "
            "venda de férias ou diárias. "
            "Chame cada especialista no máximo uma vez. "
            "Não acrescente fundamentos ou valores não verificados. "
            "Não altere as violações calculadas pelo Python. "
            "Não declare aprovação da chefia. "
            "Não divulgue salário-base ou dados restritos. "
            "Consolide em até quatro frases."
        ),
        tools=ferramentas,
        model_settings=CONFIG,
        model="llama3.1",
    )
