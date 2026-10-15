
"""
ETAPA 10 — Observabilidade.

10.2 — Mede a execução dos nós do LangGraph e registra métricas.
10.3 — Resume as métricas de uma execução pelo thread_id.

Arquivo de métricas:
    saidas/metricas.jsonl
"""

import functools
import json
import sys
import time

from dados_rh import SAIDAS


ARQUIVO_METRICAS = SAIDAS / "metricas.jsonl"


def medido(nome: str, no):
    """
    Envolve um nó do grafo para medir seu tempo de execução.

    Registra:
    - thread_id
    - nome do nó
    - duração em segundos
    - tokens, quando disponíveis
    - fonte dos feriados, quando disponível

    Retorna exatamente o resultado original do nó.
    """

    @functools.wraps(no)
    def executar_medido(estado):
        inicio = time.perf_counter()

        try:
            resultado = no(estado)
        finally:
            segundos = time.perf_counter() - inicio

            metrica = {
                "thread_id": estado.get("thread_id", ""),
                "no": nome,
                "segundos": round(segundos, 6),
            }

            if (
                "resultado" in locals()
                and isinstance(resultado, dict)
            ):
                if "tokens" in resultado:
                    metrica["tokens"] = resultado["tokens"]

                if "fonte_feriados" in resultado:
                    metrica["fonte_feriados"] = (
                        resultado["fonte_feriados"]
                    )

            SAIDAS.mkdir(
                parents=True,
                exist_ok=True,
            )

            with ARQUIVO_METRICAS.open(
                "a",
                encoding="utf-8",
            ) as arquivo:
                arquivo.write(
                    json.dumps(
                        metrica,
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )

        return resultado

    return executar_medido


def resumo(thread_id: str) -> dict:
    """
    Consulta as métricas de uma execução.

    Retorna:
    - nos: nós executados, na ordem registrada
    - segundos_total: soma dos tempos de execução
    - mais_lento: nome e duração do nó mais lento
    - tokens_total: soma dos tokens informados

    Se não houver métricas, retorna um resumo vazio.
    """

    metricas = []

    if ARQUIVO_METRICAS.exists():
        with ARQUIVO_METRICAS.open(
            "r",
            encoding="utf-8",
        ) as arquivo:
            for linha in arquivo:
                linha = linha.strip()

                if not linha:
                    continue

                try:
                    metrica = json.loads(linha)
                except json.JSONDecodeError:
                    continue

                if str(metrica.get("thread_id", "")) == str(
                    thread_id
                ):
                    metricas.append(metrica)

    if not metricas:
        return {
            "nos": [],
            "segundos_total": 0.0,
            "mais_lento": ("", 0.0),
            "tokens_total": 0,
        }

    nos = [
        metrica.get("no", "")
        for metrica in metricas
    ]

    segundos_total = sum(
        float(metrica.get("segundos", 0) or 0)
        for metrica in metricas
    )

    mais_lenta = max(
        metricas,
        key=lambda metrica: float(
            metrica.get("segundos", 0) or 0
        ),
    )

    mais_lento = (
        mais_lenta.get("no", ""),
        float(mais_lenta.get("segundos", 0) or 0),
    )

    tokens_total = sum(
        int(metrica.get("tokens", 0) or 0)
        for metrica in metricas
    )

    return {
        "nos": nos,
        "segundos_total": round(segundos_total, 6),
        "mais_lento": mais_lento,
        "tokens_total": tokens_total,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Uso: python etapa10_observabilidade.py <thread_id>"
        )
        sys.exit(1)

    print(
        json.dumps(
            resumo(sys.argv[1]),
            ensure_ascii=False,
            indent=2,
        )
    )
