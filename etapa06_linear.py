"""
ETAPA 6 — Fluxo linear de RH.

Etapa 6.3: análise pelos agentes especialistas.
Etapa 6.4: processamento completo do pedido.
"""

import json
import os
import re
import sys
from datetime import date, timedelta

from agents import RunHooks
from etapa04_ferramentas import TempoEsgotado

import etapa06_agentes as agentes
from dados.casos import CASOS
from etapa02_extracao import extrair
from etapa03_guardrails import (
    encontrar_dado_sensivel,
    nota_escopo,
    LIMIAR,
)
from etapa04_ferramentas import (
    gerar_protocolo,
    rodar_com_limites_sync,
)
from etapa05_cliente_mcp import chamar_mcp
from etapa06_regras import (
    precisa_chefia,
    resumo_periodo,
    violacoes_objetivas,
)
from provedor import configurar

MODELO = configurar()


def _completar_dados_explicitos(texto: str, pedido: dict) -> dict:
    """
    Recupera apenas matrícula e datas explicitamente presentes
    no texto, caso o extrator tenha omitido esses campos.
    """
    pedido = pedido.copy()

    if not pedido.get("matricula"):
        padrao = (
            r"\bmatr[ií]cula\s*(?:n[º°.]?\s*)?"
            r"[:#-]?\s*(\d{4})\b"
        )
        achado = re.search(padrao, texto, re.IGNORECASE)

        if achado:
            pedido["matricula"] = achado.group(1)

    datas = []

    padrao_data = (
        r"\b(\d{4}-\d{2}-\d{2})\b"
        r"|\b(\d{1,2}/\d{1,2}/\d{4})\b"
    )

    for achado in re.finditer(padrao_data, texto):
        valor = achado.group(1) or achado.group(2)

        try:
            if "/" in valor:
                dia, mes, ano = map(int, valor.split("/"))
                valor = date(ano, mes, dia).isoformat()
            else:
                valor = date.fromisoformat(valor).isoformat()

            datas.append(valor)

        except ValueError:
            continue

    if not pedido.get("data_inicio") and datas:
        pedido["data_inicio"] = datas[0]

    if not pedido.get("data_fim"):
        if pedido.get("tipo") == "abono" and pedido.get("data_inicio"):
            pedido["data_fim"] = pedido["data_inicio"]
        elif len(datas) >= 2:
            pedido["data_fim"] = datas[1]

    return pedido


def analisar(
    pedido: dict,
    servidor: dict,
    periodo: dict,
    violacoes: list[str],
    hooks: RunHooks | None = None,
) -> str:
    """Consulta o Coordenador e reúne os pareceres."""

    contexto = {
        "pedido": pedido,
        "servidor": {
            "matricula": pedido["matricula"],
            "nome": servidor.get("nome"),
            "cargo": servidor.get("cargo"),
            "equipe": servidor.get("equipe"),
            "saldo_ferias_dias": servidor.get("saldo_ferias_dias"),
            "abonos_restantes": servidor.get("abonos_restantes"),
        },
        "periodo": periodo,
        "violacoes_objetivas": violacoes,
    }

    entrada = (
        "Analise este pedido de RH utilizando os especialistas.\n"
        "Consulte Normas para explicar as regras aplicáveis.\n"
        "Consulte Escala quando o pedido envolver férias ou abono.\n"
        "Consulte Financeiro apenas quando houver venda de férias "
        "ou pedido de diárias.\n"
        "Não altere as violações objetivas calculadas pelo Python.\n"
        "Não revele o salário-base no parecer consolidado.\n"
        "Apresente os fundamentos e os cálculos pertinentes.\n\n"
        + json.dumps(contexto, ensure_ascii=False, indent=2)
    )

    resultado = rodar_com_limites_sync(
        agentes.criar_coordenador(hooks=hooks),
        entrada,
        timeout_s=35,
        hooks=hooks,
    )

    return str(resultado.final_output)


def _despacho_seguro(
    pedido,
    inicio,
    fim,
    violacoes,
    exige_chefia,
    sugestao,
):
    """Despacho objetivo, sem inferências ou dados pessoais sensíveis."""

    abertura = (
        f"Pedido de {pedido['tipo']} da matrícula "
        f"{pedido['matricula']}, para o período de "
        f"{inicio} a {fim}."
    )

    if violacoes:
        texto = (
            abertura
            + " Indefere-se em razão de: "
            + "; ".join(violacoes)
            + "."
        )

    elif exige_chefia:
        texto = (
            abertura
            + " Não foram constatadas violações objetivas. "
            "Parecer favorável condicionado à aprovação da chefia; "
            "não constitui aprovação definitiva."
        )

    else:
        texto = (
            abertura
            + " Sem violações objetivas. Defere-se o pedido."
        )

    if sugestao:
        texto += (
            " Sugere-se, após consulta à escala, o período "
            f"de {sugestao[0]} a {sugestao[1]}."
        )

    return texto


def processar(
    texto: str,
    hooks: RunHooks | None = None,
) -> dict:
    """Executa as nove etapas do fluxo linear."""

    # 1 — GUARDRAILS
    print("[1/9] Guardrails")

    sensivel = encontrar_dado_sensivel(texto)

    if sensivel:
        return {
            "resultado": "bloqueado",
            "mensagem": (
                f"Foi identificado dado sensível ({sensivel}). "
                "Remova essa informação antes de continuar."
            ),
        }

    nota, motivo = nota_escopo(texto)
    print(f"  Nota de fora do escopo: {nota:.2f}")

    if nota >= LIMIAR:
        return {
            "resultado": "bloqueado",
            "mensagem": f"Pedido fora do escopo: {motivo}",
        }

    # 2 — EXTRAÇÃO
    print("[2/9] Extração estruturada")

    pedido = extrair(texto).model_dump(mode="json")
    pedido = _completar_dados_explicitos(texto, pedido)

    print(
        "  Pedido extraído:",
        json.dumps(pedido, ensure_ascii=False),
    )

    matricula = pedido.get("matricula")
    inicio = pedido.get("data_inicio")
    fim = pedido.get("data_fim")

    if not matricula or not inicio or not fim:
        return {
            "resultado": "pedir_dados",
            "mensagem": (
                "Não foi possível identificar com segurança "
                "a matrícula e todas as datas do pedido."
            ),
            "pedido": pedido,
        }

    if pedido.get("tipo") not in ("ferias", "abono", "diaria"):
        return {
            "resultado": "pedir_dados",
            "mensagem": "Tipo de pedido não suportado por este fluxo.",
            "pedido": pedido,
        }

    try:
        data_inicial = date.fromisoformat(inicio)
        data_final = date.fromisoformat(fim)

    except (ValueError, TypeError):
        return {
            "resultado": "pedir_dados",
            "mensagem": "Datas inválidas. Utilize AAAA-MM-DD.",
        }

    if data_final < data_inicial:
        return {
            "resultado": "pedir_dados",
            "mensagem": "A data final não pode anteceder a inicial.",
        }

    if pedido["tipo"] == "diaria" and not pedido.get("tipo_destino"):
        return {
            "resultado": "pedir_dados",
            "mensagem": (
                "Informe se o destino da viagem é capital, "
                "interior ou exterior."
            ),
        }

    # 3 — SERVIDOR
    print("[3/9] Consulta MCP: servidor")

    servidor = chamar_mcp(
        "consultar_servidor",
        {"matricula": matricula},
    )

    if not isinstance(servidor, dict) or servidor.get("erro"):
        return {
            "resultado": "pedir_dados",
            "mensagem": (
                f"Matrícula {matricula} não localizada. "
                "Confira os dados informados."
            ),
        }

    # O MCP não devolve matrícula; ela vem do pedido.
    servidor = {**servidor, "matricula": matricula}

    # 4 — DATAS
    print("[4/9] Datas e feriados")

    periodo = resumo_periodo(inicio, fim)

    # 5 — ESCALA E REGRAS
    print("[5/9] Escala e regras objetivas")

    escala = chamar_mcp(
        "consultar_escala",
        {
            "equipe": servidor["equipe"],
            "inicio": inicio,
            "fim": fim,
        },
    )

    if not isinstance(escala, dict) or escala.get("erro"):
        return {
            "resultado": "erro_consulta",
            "mensagem": "Não foi possível consultar a escala.",
        }

    violacoes = violacoes_objetivas(
        pedido,
        servidor,
        periodo,
        escala,
    )

    print("  Violações:", violacoes or "nenhuma")

    decisao = "indeferido" if violacoes else "deferido"
    exige_chefia = precisa_chefia(pedido, violacoes)

    # 6 — COORDENADOR
    print("[6/9] Coordenador e especialistas")

    # Em CPU, o Coordenador pode ultrapassar o orçamento de tempo.
    # O fallback NÃO simula uma consulta de especialista:
    # registra a limitação.
    modo_reduzido = False

    try:
        parecer = analisar(
            pedido,
            servidor,
            periodo,
            violacoes,
            hooks=hooks,
        )

    except TempoEsgotado:
        modo_reduzido = True

        parecer = (
            "Parecer automatizado indisponível por limite de tempo. "
            "Conclusão baseada exclusivamente nas regras objetivas "
            "verificadas: "
            + (
                "; ".join(violacoes)
                if violacoes
                else "sem violações objetivas"
            )
        )

        print(
            "  Coordenador excedeu 35 s; "
            "usando regras objetivas, sem simular especialistas."
        )

    # Sugestão de datas somente quando uma nova consulta
    # comprova ausência de conflito.
    sugestao = None

    if any(str(v).startswith("N5") for v in violacoes):
        afastamentos = escala.get("afastamentos", [])
        datas_fim = []

        for afastamento in afastamentos:
            try:
                datas_fim.append(
                    date.fromisoformat(afastamento["fim"])
                )
            except (KeyError, ValueError, TypeError):
                pass

        if datas_fim:
            inicio_alt = max(
                max(datas_fim) + timedelta(days=1),
                data_final + timedelta(days=1),
            )

            inicio_alt += timedelta(
                days=(7 - inicio_alt.weekday()) % 7
            )

            duracao = (data_final - data_inicial).days

            for _ in range(4):
                fim_alt = inicio_alt + timedelta(days=duracao)

                try:
                    escala_alt = chamar_mcp(
                        "consultar_escala",
                        {
                            "equipe": servidor["equipe"],
                            "inicio": inicio_alt.isoformat(),
                            "fim": fim_alt.isoformat(),
                        },
                    )

                    if (
                        isinstance(escala_alt, dict)
                        and not escala_alt.get("erro")
                    ):
                        pedido_alt = {
                            **pedido,
                            "data_inicio": inicio_alt.isoformat(),
                            "data_fim": fim_alt.isoformat(),
                        }

                        periodo_alt = resumo_periodo(
                            inicio_alt.isoformat(),
                            fim_alt.isoformat(),
                        )

                        if not violacoes_objetivas(
                            pedido_alt,
                            servidor,
                            periodo_alt,
                            escala_alt,
                        ):
                            sugestao = (
                                inicio_alt.isoformat(),
                                fim_alt.isoformat(),
                            )
                            break

                except Exception as erro:
                    print(
                        "  Consulta de alternativa indisponível:",
                        erro,
                    )
                    break

                inicio_alt += timedelta(days=7)

    # 7 — REDATOR
    print("[7/9] Redator")

    fatos = {
        "matricula": matricula,
        "nome": servidor.get("nome"),
        "tipo": pedido["tipo"],
        "inicio": inicio,
        "fim": fim,
        "dias_vendidos": pedido.get("dias_vendidos", 0),
        "destino": pedido.get("destino"),
        "tipo_destino": pedido.get("tipo_destino"),
        "periodo": periodo,
        "decisao": decisao,
        "pendente_chefia": exige_chefia,
        "violacoes": violacoes,
        "parecer": parecer,
    }

    entrada_redator = (
        "Redija um despacho administrativo usando apenas "
        "os fatos apresentados abaixo.\n"
        "Respeite a decisão determinada pelo Python.\n"
        "Se houver violações, fundamente o indeferimento.\n"
        "Se houver necessidade de aprovação da chefia, "
        "indique expressamente que a decisão ainda está pendente.\n"
        "Não declare aprovação da chefia sem autorização humana.\n"
        "Não inclua CPF, CID nem informações médicas.\n"
        "Não invente valores nem datas alternativas.\n\n"
        + json.dumps(fatos, ensure_ascii=False, indent=2)
    )

    if modo_reduzido:
        despacho = _despacho_seguro(
            pedido,
            inicio,
            fim,
            violacoes,
            exige_chefia,
            sugestao,
        )

    else:
        try:
            despacho = str(
                rodar_com_limites_sync(
                    agentes.criar_redator(),
                    entrada_redator,
                    hooks=hooks,
                    timeout_s=35,
                ).final_output
            )

        except TempoEsgotado:
            print(
                "  Redator excedeu 35 s; "
                "usando despacho objetivo."
            )

            despacho = _despacho_seguro(
                pedido,
                inicio,
                fim,
                violacoes,
                exige_chefia,
                sugestao,
            )

    # A decisão e a fundamentação objetiva
    # nunca dependem do texto do LLM.
    if (
        encontrar_dado_sensivel(despacho)
        or (
            violacoes
            and not all(
                v.split(":", 1)[0] in despacho
                for v in violacoes
            )
        )
        or (
            exige_chefia
            and "chefia" not in despacho.lower()
        )
    ):
        despacho = _despacho_seguro(
            pedido,
            inicio,
            fim,
            violacoes,
            exige_chefia,
            sugestao,
        )

    if sugestao and sugestao[0] not in despacho:
        despacho += (
            " Sugere-se, após consulta à escala, o período "
            f"de {sugestao[0]} a {sugestao[1]}."
        )

    # 8 — PROTOCOLO
    print("[8/9] Geração do protocolo")

    protocolo = gerar_protocolo(
        matricula,
        pedido["tipo"],
        inicio,
        fim,
    )

    print("  Protocolo:", protocolo)

    # 9 — REGISTRO
    print("[9/9] Registro da decisão")

    aprovador = (
        "pendente_chefia"
        if exige_chefia
        else "sistema"
    )

    registro = chamar_mcp(
        "registrar_decisao",
        {
            "protocolo": protocolo,
            "matricula": matricula,
            "tipo": pedido["tipo"],
            "inicio": inicio,
            "fim": fim,
            "decisao": decisao,
            "aprovador": aprovador,
            "despacho": despacho,
            "token": os.getenv("TOKEN_REGISTRO", ""),
        },
    )

    if not isinstance(registro, dict) or registro.get("erro"):
        return {
            "resultado": "erro_registro",
            "protocolo": protocolo,
            "mensagem": (
                registro.get("erro", "Falha no registro.")
                if isinstance(registro, dict)
                else "Resposta inválida do MCP."
            ),
            "despacho": despacho,
            "parecer": parecer,
        }

    if registro.get("sucesso") is False:
        return {
            "resultado": "erro_registro",
            "protocolo": protocolo,
            "mensagem": "O servidor MCP recusou o registro.",
            "registro": registro,
        }

    return {
        "resultado": decisao,
        "pendente_chefia": exige_chefia,
        "sugestao_datas": sugestao,
        "protocolo": protocolo,
        "despacho": despacho,
        "parecer": parecer,
        "violacoes": violacoes,
        "registro": registro,
    }


def testar_governanca() -> None:
    """Bônus B.4: ainda não implementado."""

    raise NotImplementedError(
        "Os testes opcionais de governança ficam para o bônus B."
    )


if __name__ == "__main__":
    print(f"[modelo] {MODELO}")

    if "--governanca" in sys.argv:
        testar_governanca()

    else:
        for nome in (
            "C1_abono_simples",
            "C2_ferias_com_venda",
            "C6_conflito_de_escala",
            "C11_diaria_exterior",
        ):
            print(f"\n=============== {nome}")

            resultado = processar(
                CASOS[nome]["pedido"]
            )

            print(
                json.dumps(
                    resultado,
                    ensure_ascii=False,
                    indent=2,
                )
            )