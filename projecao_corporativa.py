"""
╔══════════════════════════════════════════════════════════════════╗
║  CÁLCULO DE PROJEÇÃO CORPORATIVA                                ║
║  Projeto Piloto IA — EQTL                                       ║
║                                                                  ║
║  Como usar:                                                     ║
║  python projecao_corporativa.py                                 ║
║      --tasks        EQUATORIAL_Tasks.csv                        ║
║      --curva        CURVA_LB0.csv                               ║
║      --sep          ,   (ou ; se exportou com ponto e vírgula)  ║
║      --data_inicio  2026-04-01  (processa a partir desta data)  ║
║      --data_fim     2026-04-30  (processa até esta data)        ║
║      --fator_compressao  0.6   (fator de compactação 0-1)       ║
╚══════════════════════════════════════════════════════════════════╝

Modos de projeção gerados (sempre AMBOS no CSV/Excel):
  REALISTA    — durações reais respeitando predecessoras (pode atrasar)
  COMPACTADO  — tenta comprimir tarefas para caber no prazo da obra

No dashboard, o usuário escolhe qual cenário visualizar via toggle.
"""

import argparse
import warnings
import re
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
from datetime import timedelta

# ──────────────────────────────────────────────
# CONFIGURAÇÕES
# ──────────────────────────────────────────────
HORAS_POR_DIA  = 8
VELOCIDADE_MIN = 0.05
SEP = "─" * 65

# Mapeamento de tipos de predecessora não padrão
# II = Inicio-Inicio  → SS
# TT = Termino-Termino → FF
# TI = Termino-Inicio  → FS
# IT = variante de SS  → SS
TIPO_MAP = {
    'II': 'SS',
    'IT': 'SS',
    'TI': 'FS',
    'TT': 'FF',
}

def log(msg): print(msg)
def sep(t=""): print(f"\n{SEP}\n  {t}\n{SEP}" if t else SEP)


def pct_concluido(row):
    """Retorna % de conclusao. Prioriza TaskPercentWorkCompleted."""
    work = float(row.get('TaskPercentWorkCompleted') or 0)
    if work > 0:
        return work
    return float(row.get('TaskPercentCompleted') or 0)


def calcular_status_curva(realizado, planejado, baseline_finish, anomes_ref):
    """
    Replica logica DAX do BI corporativo para STATUS da tarefa na Curva S.
    realizado   = DIAS_PCT_MES_ACUMULADO realizado (0-1)
    planejado   = DIAS_PCT_MES_ACUMULADO planejado LB (0-1)
    baseline_finish = TaskBaselineFinishDate
    anomes_ref  = mes de referencia (Timestamp)
    """
    if realizado >= 1.0:
        return 'Concluido'
    if realizado == 0 and planejado == 0:
        return 'Nao Iniciado'
    if realizado > 0 and planejado == 0:
        return 'Adiantado'

    ratio = realizado / planejado if planejado > 0 else 0.0

    if ratio > 1.0:
        return 'Adiantado'

    # Baseline ja passou do inicio do mes de referencia
    if pd.notna(baseline_finish) and pd.notna(anomes_ref):
        inicio_mes = pd.Timestamp(anomes_ref).replace(day=1)
        if pd.to_datetime(baseline_finish) < inicio_mes:
            return 'Atrasado'

    if ratio < 0.80:
        return 'Atrasado'
    if ratio < 0.90:
        return 'Alerta'
    return 'No Prazo'



# ──────────────────────────────────────────────
# 1. LEITURA DOS ARQUIVOS
# ──────────────────────────────────────────────

def ler_csv(caminho, separador=','):
    log(f"  Lendo: {caminho}")
    encodings = ['utf-8-sig', 'utf-8', 'cp1252', 'latin-1']
    for enc in encodings:
        try:
            df = pd.read_csv(
                caminho, sep=separador, encoding=enc,
                on_bad_lines='skip', low_memory=False, dtype=str,
            )
            df.columns = limpar_colunas(df.columns)
            if len(df.columns) <= 1:
                continue
            log(f"  Encoding: {enc} | {len(df)} linhas x {len(df.columns)} colunas")
            return df
        except Exception:
            continue
    raise RuntimeError(f"Nao foi possivel ler: {caminho}")


def limpar_colunas(colunas):
    return [c.strip().strip('"').strip(';').strip('"').strip() for c in colunas]


def converter_datas(df, colunas):
    for col in colunas:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')
    return df


COLUNAS_DATA_TASKS = [
    'TaskStartDate', 'TaskFinishDate',
    'TaskActualStartDate', 'TaskActualFinishDate',
    'TaskBaselineStartDate', 'TaskBaselineFinishDate',
    'INICIO_PROJECAO', 'TERMINO_PROJECAO',
    # ✅ CORRIGIDO: fonte de projeção lançada = PROJECAO_LANCADO (não CORPORATIVA_LANCADO)
    'INICIO_PROJECAO_LANCADO', 'TERMINO_PROJECAO_LANCADO',
    'INICIO_PROJECAO_CORPORATIVA', 'TERMINO_PROJECAO_CORPORATIVA',
    'INICIO_PROJECAO_CORPORATIVA_LANCADO', 'TERMINO_PROJECAO_CORPORATIVA_LANCADO',
    'INICIO_PROJECAO_CORPORATIVA_CALCULADO', 'TERMINO_PROJECAO_CORPORATIVA_CALCULADO',
    'INICIO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
    'TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
    'Inicio_LB', 'Termino_LB',
    # ✅ CORRIGIDO: datas reais de início/fim = Assignments (não Task*)
    'Assignments_Inicio_Real_Ajustado', 'Assignments_Termino_Real_Ajustado',
    'Assignments_Inicio_Real_Digitado', 'Assignments_Termino_Real_Digitado',
]

COLUNAS_DATA_CURVA = [
    'TaskActualStartDate', 'TaskActualFinishDate',
]


# ──────────────────────────────────────────────
# 2. FILTRO POR PERÍODO (novo)
# ──────────────────────────────────────────────

def filtrar_por_periodo(tasks_df, data_inicio=None, data_fim=None):
    """
    Filtra tarefas cujo início ou término planejado/lançado
    caia dentro do período informado.
    Tarefas em andamento (0 < pct < 100) são sempre incluídas.
    Se nenhum filtro informado, retorna tudo.
    """
    if data_inicio is None and data_fim is None:
        return tasks_df

    mascara = pd.Series(False, index=tasks_df.index)

    # Tarefas em andamento → sempre incluir
    pct = pd.to_numeric(tasks_df.get('TaskPercentCompleted', pd.Series(dtype=float)),
                        errors='coerce').fillna(0)
    mascara |= (pct > 0) & (pct < 100)

    # Datas relevantes para o filtro de período
    for col in ['Assignments_Inicio_Real_Ajustado', 'TaskStartDate',
                'INICIO_PROJECAO_LANCADO', 'TERMINO_PROJECAO_LANCADO']:
        if col not in tasks_df.columns:
            continue
        datas = pd.to_datetime(tasks_df[col], errors='coerce')
        if data_inicio is not None:
            mascara |= datas >= pd.Timestamp(data_inicio)
        if data_fim is not None:
            mascara |= datas <= pd.Timestamp(data_fim)

    n_antes = len(tasks_df)
    resultado = tasks_df[mascara].copy()
    log(f"  Filtro de período [{data_inicio} → {data_fim}]: {n_antes} → {len(resultado)} tarefas")
    return resultado


# ──────────────────────────────────────────────
# 3. VELOCIDADE REAL DA CURVA S
# ──────────────────────────────────────────────

def calcular_velocidade_curva(tasks_df, curva_df):
    hoje = pd.Timestamp.today().normalize()
    tres_meses_atras = hoje - pd.DateOffset(months=3)

    if 'ANOMES' in curva_df.columns:
        curva_df['ANOMES'] = pd.to_datetime(
            curva_df['ANOMES'].astype(str).str.replace('/', '-') + '-01',
            errors='coerce'
        )

    for col in ['DIAS_PCT_MES', 'DIAS_PCT_MES_ACUMULADO']:
        if col in curva_df.columns:
            curva_df[col] = pd.to_numeric(curva_df[col], errors='coerce').fillna(0)

    # Separa planejado (TIPO='LB') de realizado (demais tipos)
    # Velocidade é calculada sobre o realizado
    if 'TIPO' in curva_df.columns:
        curva_realizado = curva_df[curva_df['TIPO'] != 'LB'].copy()
        curva_planejado = curva_df[curva_df['TIPO'] == 'LB'].copy()
    else:
        curva_realizado = curva_df.copy()
        curva_planejado = curva_df.copy()

    curva_passado = curva_realizado[curva_realizado['ANOMES'] <= hoje].copy()

    # Velocidade recente: últimos 3 meses (sobre realizado)
    curva_recente = curva_passado[curva_passado['ANOMES'] >= tres_meses_atras].copy()
    vel_recente = curva_recente.groupby('TaskId').agg(
        pct_mensal_recente=('DIAS_PCT_MES', lambda x: x[x > 0].mean() if (x > 0).any() else 0),
    ).reset_index()

    # Velocidade histórica: todo o histórico
    vel = curva_passado.groupby('TaskId').agg(
        meses_planejados=('DIAS_PCT_MES', 'count'),
        meses_com_avanco=('DIAS_PCT_MES', lambda x: (x > 0).sum()),
        pct_planejado_acumulado=('DIAS_PCT_MES_ACUMULADO', 'max'),
        pct_mensal_historico=('DIAS_PCT_MES', lambda x: x[x > 0].mean() if (x > 0).any() else 0),
    ).reset_index()

    # Média ponderada: 60% recente + 40% histórico
    vel = vel.merge(vel_recente, on='TaskId', how='left')
    vel['pct_mensal_medio'] = vel.apply(
        lambda r: (
            0.6 * r['pct_mensal_recente'] + 0.4 * r['pct_mensal_historico']
            if pd.notna(r.get('pct_mensal_recente')) and r.get('pct_mensal_recente', 0) > 0
            else r['pct_mensal_historico']
        ),
        axis=1
    )

    # ✅ CORRIGIDO: início real = Assignments_Inicio_Real_Ajustado → fallback TaskStartDate
    tasks_slim = tasks_df[['TaskId', 'TaskPercentCompleted',
                            'Assignments_Inicio_Real_Ajustado',
                            'TaskStartDate']].copy()
    vel = vel.merge(tasks_slim, on='TaskId', how='left')

    vel['data_inicio'] = pd.to_datetime(
        vel['Assignments_Inicio_Real_Ajustado'].fillna(vel['TaskStartDate']),
        errors='coerce'
    )
    vel['dias_decorridos'] = (hoje - vel['data_inicio']).dt.days.clip(lower=1)
    vel['pct_real'] = pd.to_numeric(vel['TaskPercentCompleted'], errors='coerce').fillna(0)

    vel['velocidade_dia'] = np.where(
        vel['pct_mensal_medio'] > 0,
        vel['pct_mensal_medio'] / 30.0,
        np.where(
            vel['dias_decorridos'] > 0,
            vel['pct_real'] / vel['dias_decorridos'],
            VELOCIDADE_MIN
        )
    )
    vel['velocidade_dia'] = vel['velocidade_dia'].clip(lower=VELOCIDADE_MIN)

    return vel[['TaskId', 'velocidade_dia', 'pct_real',
                'meses_com_avanco', 'pct_planejado_acumulado',
                'pct_mensal_medio']].set_index('TaskId')


# ──────────────────────────────────────────────
# 4. PARSEAR PREDECESSORAS
# ──────────────────────────────────────────────

def parsear_predecessoras(pred_str):
    """
    Parseia PredecessoraBI. Exemplos suportados:
      '6'              → tarefa 6, tipo FS, lag 0d
      '14TI+30d'       → tarefa 14, tipo FS (TI→FS), lag +30 dias
      '3II+50%'        → tarefa 3, tipo SS (II→SS), lag +50% da duração da pred
      '37II+20%;20TT+5d' → duas predecessoras separadas por ';'

    ✅ CORRIGIDO: lag em % é convertido para dias no forward_pass,
    usando TaskBaselineDuration da predecessora (comportamento MS Project).
    """
    if pd.isna(pred_str) or str(pred_str).strip() in ('', 'nan'):
        return []
    resultado = []
    for parte in str(pred_str).split(';'):
        parte = parte.strip()
        if not parte:
            continue
        m = re.match(r'(\d+)([A-Z]{2})?([+\-]\d+\.?\d*)?(%|d)?', parte, re.IGNORECASE)
        if m:
            tipo_raw = (m.group(2) or 'FS').upper()
            tipo     = TIPO_MAP.get(tipo_raw, tipo_raw)
            lag_val  = float(m.group(3) or 0)
            lag_unit = (m.group(4) or 'd').lower()
            resultado.append({
                'codigo':  int(m.group(1)),
                'tipo':    tipo,
                'lag':     lag_val,
                'lag_pct': lag_unit == '%',  # True → lag é % da duração da predecessora
            })
    return resultado


# ──────────────────────────────────────────────
# 5. DURAÇÃO RESTANTE
# ──────────────────────────────────────────────

def duracao_restante(row, vel_map):
    pct = pct_concluido(row)
    if pct >= 100:
        return 0.0

    task_id      = row.get('TaskId', '')
    restante_pct = 100.0 - pct
    hoje         = pd.Timestamp.today().normalize()

    # ✅ CORRIGIDO: âncora de início = Assignments_Inicio_Real_Ajustado → fallback TaskStartDate
    ini_real = pd.to_datetime(row.get('Assignments_Inicio_Real_Ajustado'), errors='coerce')
    ini_plan = pd.to_datetime(row.get('TaskStartDate'), errors='coerce')
    ini_ref  = ini_real if pd.notna(ini_real) else ini_plan

    # Tarefa não iniciada com data futura → usa duração do baseline
    if pct == 0 and pd.notna(ini_ref) and ini_ref >= hoje:
        dur_lb = float(row.get('TaskBaselineDuration') or 0)
        if dur_lb > 0:
            return max(dur_lb / HORAS_POR_DIA, 1.0)
        dur = float(row.get('TaskDuration') or 0)
        if dur > 0:
            return max(dur / HORAS_POR_DIA, 1.0)
        return 1.0

    # Tarefa em andamento: usa velocidade real da Curva S
    if task_id in vel_map.index:
        vel = vel_map.loc[task_id, 'velocidade_dia']
        if vel > VELOCIDADE_MIN:
            return max(restante_pct / vel, 1.0)

    dur_lb = float(row.get('TaskBaselineDuration') or 0)
    if dur_lb > 0:
        return max((dur_lb / HORAS_POR_DIA) * (restante_pct / 100.0), 1.0)

    dur = float(row.get('TaskDuration') or 0)
    if dur > 0:
        return max((dur / HORAS_POR_DIA) * (restante_pct / 100.0), 1.0)

    return 1.0


def _dias_restantes_baseline(row):
    """Dias restantes pelo baseline — usado quando projeção cai no passado."""
    pct      = float(row.get('TaskPercentCompleted') or 0)
    restante = 100.0 - pct
    dur_lb   = float(row.get('TaskBaselineDuration') or 0)
    dur_tk   = float(row.get('TaskDuration') or 0)
    dur_base = dur_lb if dur_lb > 0 else dur_tk
    if dur_base > 0:
        return max((dur_base / HORAS_POR_DIA) * (restante / 100.0), 1.0)
    return 30.0  # fallback conservador


# ──────────────────────────────────────────────
# 6. FORWARD PASS  (realista + compactado)
# ──────────────────────────────────────────────

def forward_pass(grupo, vel_map, data_ini_obra, data_fim_lb, fator_compressao=0.6):
    """
    Executa o forward pass calculando DOIS cenários simultaneamente:

      _ini_calc / _fim_calc  → REALISTA  (durações reais, aceita ultrapassar prazo)
      _ini_comp / _fim_comp  → COMPACTADO (tenta comprimir para caber no prazo da obra)

    Cada cenário respeita as predecessoras do seu próprio cenário, garantindo
    que o efeito cascata da compressão seja propagado corretamente.

    Correções aplicadas:
      ✅ Projeção lançada: lê INICIO/TERMINO_PROJECAO_LANCADO
      ✅ Âncora de início: Assignments_Inicio_Real_Ajustado → fallback TaskStartDate
      ✅ Lag em %: converte para dias usando duração da predecessora (MS Project behavior)
    """
    hoje  = pd.Timestamp.today().normalize()
    grupo = grupo.copy().sort_values('TaskIndex').reset_index(drop=True)

    # Mapa TaskIndex → posição no dataframe
    idx_map = {}
    for i, row in grupo.iterrows():
        try:
            idx_map[int(float(row['TaskIndex']))] = i
        except Exception:
            pass

    for col in ['_ini_calc', '_fim_calc', '_ini_comp', '_fim_comp']:
        grupo[col] = pd.NaT
    grupo['_origem_real'] = ''
    grupo['_origem_comp'] = ''
    grupo['_atrasa']      = False

    # ── helpers: lêem o cenário já calculado para propagar predecessoras ──
    def _get(col, codigo, fallback):
        i = idx_map.get(int(codigo))
        if i is None:
            return fallback
        v = grupo.at[i, col]
        return v if pd.notna(v) else fallback

    def get_fim_real(c): return _get('_fim_calc', c, data_ini_obra)
    def get_ini_real(c): return _get('_ini_calc', c, data_ini_obra)
    def get_fim_comp(c): return _get('_fim_comp', c, data_ini_obra)
    def get_ini_comp(c): return _get('_ini_comp', c, data_ini_obra)

    def resolver_lag_dias(pred):
        """
        ✅ CORRIGIDO: converte lag em % para dias usando duração da predecessora.
        Lag em dias retorna diretamente.
        """
        lag_val = pred.get('lag', 0)
        if pred.get('lag_pct', False):
            i_pred = idx_map.get(int(pred['codigo']))
            if i_pred is not None:
                dur_pred = float(
                    grupo.at[i_pred, 'TaskBaselineDuration'] or
                    grupo.at[i_pred, 'TaskDuration'] or 0
                )
                lag_val = (dur_pred / HORAS_POR_DIA) * (lag_val / 100.0)
            else:
                lag_val = 0.0
        return lag_val

    def aplicar_pred(pred, get_ini_fn, get_fim_fn):
        """Calcula candidato a início a partir de uma predecessora."""
        lag  = resolver_lag_dias(pred)
        tipo = pred.get('tipo', 'FS')
        if tipo == 'FS':
            return get_fim_fn(pred['codigo']) + timedelta(days=lag)
        elif tipo == 'SS':
            return get_ini_fn(pred['codigo']) + timedelta(days=lag)
        elif tipo == 'FF':
            return get_fim_fn(pred['codigo']) + timedelta(days=lag) - timedelta(days=1)
        else:
            return get_fim_fn(pred['codigo']) + timedelta(days=lag)

    def inicio_via_predecessoras(preds, get_ini_fn, get_fim_fn):
        inicio = data_ini_obra
        for pred in preds:
            try:
                candidato = aplicar_pred(pred, get_ini_fn, get_fim_fn)
                inicio = max(inicio, candidato)
            except Exception:
                pass
        return inicio

    # ══════════════════════════════════════════════════════════
    # LOOP PRINCIPAL — processa uma tarefa por vez, em ordem
    # ══════════════════════════════════════════════════════════
    for i, row in grupo.iterrows():
        pct    = pct_concluido(row)
        is_sum = int(float(row.get('TaskIsSummary') or 0))
        _comp_fixo = False  # flag: compactado já foi fixado pelo lançado no passado

        # ── 1. Projeção lançada ───────────────────────────────────────
        # ✅ CORRIGIDO: fonte = INICIO/TERMINO_PROJECAO_LANCADO
        ini_lc = row.get('INICIO_PROJECAO_LANCADO')
        fim_lc = row.get('TERMINO_PROJECAO_LANCADO')
        if pd.notna(fim_lc) and pd.notna(ini_lc):
            fim_lc_dt = pd.to_datetime(fim_lc, errors='coerce')
            ini_lc_dt = pd.to_datetime(ini_lc, errors='coerce')
            lancado_valido = pd.notna(fim_lc_dt) and (fim_lc_dt >= hoje or pct >= 100 or is_sum)

            if lancado_valido:
                if pd.notna(fim_lc_dt) and fim_lc_dt >= hoje:
                    # Lançado futuro — COMPACTADO sempre mantém como meta
                    grupo.at[i, '_ini_comp']    = ini_lc_dt
                    grupo.at[i, '_fim_comp']    = fim_lc_dt
                    grupo.at[i, '_origem_comp'] = 'lancado'

                    # REALISTA — verifica se predecessoras não atrapalham o lançado
                    # Se alguma predecessora calculada termina depois do início lançado → recalcula
                    preds = parsear_predecessoras(row.get('PredecessoraBI'))
                    pred_ini_forcado = inicio_via_predecessoras(preds, get_ini_real, get_fim_real)

                    if pred_ini_forcado > ini_lc_dt:
                        # Predecessora empurra além do lançado — recalcula no realista
                        dur_lb = float(row.get('TaskBaselineDuration') or row.get('TaskDuration') or 0)
                        dias   = max(dur_lb / HORAS_POR_DIA, 1.0) if dur_lb > 0 else 1.0
                        grupo.at[i, '_ini_calc']    = pred_ini_forcado
                        grupo.at[i, '_fim_calc']    = pred_ini_forcado + timedelta(days=dias)
                        grupo.at[i, '_origem_real'] = 'calculado_atrasa'
                    else:
                        # Predecessoras OK — mantém lançado no realista
                        grupo.at[i, '_ini_calc']    = ini_lc_dt
                        grupo.at[i, '_fim_calc']    = fim_lc_dt
                        grupo.at[i, '_origem_real'] = 'lancado'
                    continue  # ambos cenários já gravados
                else:
                    # Lançado no passado:
                    # COMPACTADO → mantém como meta/compromisso
                    grupo.at[i, '_ini_comp']    = ini_lc_dt
                    grupo.at[i, '_fim_comp']    = fim_lc_dt
                    grupo.at[i, '_origem_comp'] = 'lancado'
                    # REALISTA → recalcula (cai no fluxo abaixo)
                    _comp_fixo = True  # impede que o fluxo abaixo sobrescreva o compactado
            # Lançado no passado + tarefa em andamento → realista recalcula

        # ── 2. Concluída ─────────────────────────────────────────────
        # Nunca calcula projeção. Apenas repete datas reais dos Assignments
        # para que predecessoras das sucessoras possam se ancorar nelas.
        # Se não houver Assignments, deixa NaT — projeção fica em branco.
        if pct >= 100:
            ini_r = row.get('Assignments_Inicio_Real_Ajustado') or row.get('TaskActualStartDate')
            fim_r = row.get('Assignments_Termino_Real_Ajustado') or row.get('TaskActualFinishDate')
            ini_dt = pd.to_datetime(ini_r, errors='coerce') if pd.notna(ini_r) else pd.NaT
            fim_dt = pd.to_datetime(fim_r, errors='coerce') if pd.notna(fim_r) else pd.NaT
            # Propaga para cálculo interno de predecessoras (usa data_ini_obra se NaT)
            grupo.at[i, '_ini_calc'] = ini_dt if pd.notna(ini_dt) else data_ini_obra
            grupo.at[i, '_fim_calc'] = fim_dt if pd.notna(fim_dt) else data_ini_obra
            grupo.at[i, '_ini_comp'] = ini_dt if pd.notna(ini_dt) else data_ini_obra
            grupo.at[i, '_fim_comp'] = fim_dt if pd.notna(fim_dt) else data_ini_obra
            origem_concl = 'realizado' if pd.notna(ini_dt) and pd.notna(fim_dt) else 'realizado_sem_data'
            grupo.at[i, '_origem_real'] = origem_concl
            grupo.at[i, '_origem_comp'] = origem_concl
            continue

        # ── 3. Resumo (tarefa macro) ──────────────────────────────────
        # Nunca tem projeção própria — depende exclusivamente das filhas.
        # Datas internas preenchidas depois pelo recálculo de resumos.
        # Colunas de projeção ficam sempre em branco no output final.
        if is_sum:
            grupo.at[i, '_origem_real'] = 'resumo'
            grupo.at[i, '_origem_comp'] = 'resumo'
            continue

        # ── 4. Calcular ponto de partida do início ────────────────────
        # ✅ CORRIGIDO: âncora = Assignments_Inicio_Real_Ajustado → fallback TaskStartDate
        ini_real_ajust = pd.to_datetime(row.get('Assignments_Inicio_Real_Ajustado'), errors='coerce')
        ini_plan       = pd.to_datetime(row.get('TaskStartDate'), errors='coerce')
        ini_ref        = ini_real_ajust if pd.notna(ini_real_ajust) else ini_plan

        preds = parsear_predecessoras(row.get('PredecessoraBI'))

        if pct == 0 and pd.notna(ini_ref) and ini_ref >= hoje - timedelta(days=7):
            # Tarefa não iniciada com data futura → ancora na data de referência
            # Não aplica predecessoras: a data planejada já as considera
            inicio_real = ini_ref
            inicio_comp = ini_ref
        else:
            # Tarefa em andamento ou sem data planejada futura:
            # parte do início real e propaga predecessoras do seu cenário
            base = ini_ref if (pd.notna(ini_ref) and pct > 0) else data_ini_obra

            pred_real = inicio_via_predecessoras(preds, get_ini_real, get_fim_real)
            pred_comp = inicio_via_predecessoras(preds, get_ini_comp, get_fim_comp)

            inicio_real = max(base, pred_real)
            inicio_comp = max(base, pred_comp)

        dur = duracao_restante(row, vel_map)

        # ══════════════
        # CENÁRIO REALISTA
        # ══════════════
        fim_real    = inicio_real + timedelta(days=max(dur, 1))
        origem_real = 'calculado'

        if fim_real < hoje and pct < 100:
            # Projeção caiu no passado: recalcula a partir de hoje com baseline
            # Inclui pct==0: tarefa não iniciada mas com data planejada já vencida
            inicio_real = hoje
            fim_real    = hoje + timedelta(days=_dias_restantes_baseline(row))
            origem_real = 'calculado_atraso_andamento'
        elif pd.notna(data_fim_lb) and fim_real > data_fim_lb:
            origem_real = 'calculado_atrasa'

        # ══════════════
        # CENÁRIO COMPACTADO
        # ══════════════
        fim_comp    = inicio_comp + timedelta(days=max(dur, 1))
        origem_comp = 'calculado'

        if fim_comp < hoje and pct < 100:
            # Projeção caiu no passado — recalcula de hoje
            # Inclui pct==0: tarefa não iniciada mas com data planejada já vencida
            inicio_comp = hoje
            dias_base   = _dias_restantes_baseline(row)
            # No compactado: tenta comprimir para caber no prazo da obra
            if pd.notna(data_fim_lb) and dias_base > 1:
                dias_comp = dias_base * fator_compressao
                fim_tenta = hoje + timedelta(days=max(dias_comp, 1))
                if fim_tenta <= data_fim_lb:
                    fim_comp    = fim_tenta
                    origem_comp = 'calculado_comprimido'
                else:
                    fim_comp    = hoje + timedelta(days=max(dias_base, 1))
                    origem_comp = 'calculado_atraso_andamento'
            else:
                fim_comp    = hoje + timedelta(days=max(dias_base, 1))
                origem_comp = 'calculado_atraso_andamento'
        elif pd.notna(data_fim_lb) and fim_comp > data_fim_lb and dur > 1:
            # Tenta comprimir para caber no prazo
            dur_comp  = dur * fator_compressao
            fim_tenta = inicio_comp + timedelta(days=max(dur_comp, 1))
            if fim_tenta <= data_fim_lb:
                fim_comp    = fim_tenta
                origem_comp = 'calculado_comprimido'
            else:
                # Mesmo comprimido não cabe
                origem_comp = 'calculado_atrasa'

        grupo.at[i, '_ini_calc']    = inicio_real
        grupo.at[i, '_fim_calc']    = fim_real
        grupo.at[i, '_origem_real'] = origem_real
        # Compactado só é gravado se não foi fixado pelo lançado no passado
        if not _comp_fixo:
            grupo.at[i, '_ini_comp']    = inicio_comp
            grupo.at[i, '_fim_comp']    = fim_comp
            grupo.at[i, '_origem_comp'] = origem_comp

    # ── 5. Recalcula resumos pelas filhas (ambos os cenários) ─────────
    try:
        niveis = int(grupo['TaskOutlineLevel'].max() or 1)
        for nivel in range(niveis, 0, -1):
            resumos = grupo[
                (grupo['TaskIsSummary'].astype(float) == 1) &
                (grupo['TaskOutlineLevel'].astype(float) == nivel)
            ]
            for ri, resumo in resumos.iterrows():
                task_id = resumo.get('TaskId')
                filhos  = grupo[grupo['ParentTaskId'] == task_id]
                if len(filhos) == 0:
                    continue
                for ini_col, fim_col, orig_col in [
                    ('_ini_calc', '_fim_calc', '_origem_real'),
                    ('_ini_comp', '_fim_comp', '_origem_comp'),
                ]:
                    ini_f = filhos[ini_col].dropna()
                    fim_f = filhos[fim_col].dropna()
                    if len(ini_f):
                        grupo.at[ri, ini_col] = ini_f.min()
                    if len(fim_f):
                        grupo.at[ri, fim_col] = fim_f.max()
                    grupo.at[ri, orig_col] = 'resumo_calculado'
    except Exception:
        pass

    if pd.notna(data_fim_lb):
        grupo['_atrasa'] = grupo['_fim_calc'] > data_fim_lb

    return grupo


# ──────────────────────────────────────────────
# 7. PROCESSAR TODAS AS OBRAS
# ──────────────────────────────────────────────

def processar(tasks_df, vel_map, fator_compressao=0.6):
    sep("PROCESSANDO OBRAS")

    resultados   = []
    resumo_obras = []
    obras = tasks_df['ProjectName'].unique()
    log(f"  Total de obras: {len(obras)}")
    log(f"  Fator de compressão: {fator_compressao}")

    for projeto in obras:
        grupo = tasks_df[tasks_df['ProjectName'] == projeto].copy()

        # ✅ CORRIGIDO: data início da obra = Assignments_Inicio_Real_Ajustado → fallback TaskStartDate
        data_ini = pd.to_datetime(grupo['Assignments_Inicio_Real_Ajustado'].min(), errors='coerce')
        if pd.isna(data_ini):
            data_ini = pd.to_datetime(grupo['TaskStartDate'].min(), errors='coerce')
        if pd.isna(data_ini):
            data_ini = pd.to_datetime(grupo['Inicio_LB'].min(), errors='coerce')
        if pd.isna(data_ini):
            data_ini = pd.Timestamp.today().normalize()

        data_fim_lb = pd.to_datetime(grupo['Termino_LB'].max(), errors='coerce')

        grupo = forward_pass(grupo, vel_map, data_ini, data_fim_lb, fator_compressao)

        hoje_proc = pd.Timestamp.today().normalize()

        pct_col  = pd.to_numeric(grupo['TaskPercentCompleted'], errors='coerce').fillna(0)
        is_sum_col = pd.to_numeric(grupo['TaskIsSummary'], errors='coerce').fillna(0)

        # ── Tarefas concluídas ────────────────────────────────────────
        # Não calculam projeção. Repetem datas reais dos Assignments quando
        # disponíveis. Se não houver Assignments, todas as colunas ficam em
        # branco — não herda, não inventa, não calcula.
        # Concluida: usa TaskPercentWorkCompleted se disponivel, senao TaskPercentCompleted
        work_col  = pd.to_numeric(grupo.get('TaskPercentWorkCompleted', pd.Series(0, index=grupo.index)), errors='coerce').fillna(0)
        pct_efetivo = work_col.where(work_col > 0, pct_col)
        mask_concluida = pct_efetivo >= 100

        ini_real_col = pd.to_datetime(grupo['Assignments_Inicio_Real_Ajustado'], errors='coerce')
        fim_real_col = pd.to_datetime(grupo['Assignments_Termino_Real_Ajustado'], errors='coerce')
        # Fallback para ActualStart/Finish somente se Assignments ausentes
        ini_real_col = ini_real_col.fillna(
            pd.to_datetime(grupo['TaskActualStartDate'], errors='coerce'))
        fim_real_col = fim_real_col.fillna(
            pd.to_datetime(grupo['TaskActualFinishDate'], errors='coerce'))

        # Grava datas reais quando existem; NaT quando não existem
        grupo.loc[mask_concluida, 'INICIO_PROJECAO_CORPORATIVA']  = ini_real_col[mask_concluida]
        grupo.loc[mask_concluida, 'TERMINO_PROJECAO_CORPORATIVA'] = fim_real_col[mask_concluida]

        # Origem: 'realizado' com datas, 'realizado_sem_data' sem datas
        tem_datas = mask_concluida & ini_real_col.notna() & fim_real_col.notna()
        sem_datas = mask_concluida & ~(ini_real_col.notna() & fim_real_col.notna())
        grupo.loc[tem_datas, 'ORIGEM_PROJECAO_CORPORATIVA_REALISTA']   = 'realizado'
        grupo.loc[tem_datas, 'ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO'] = 'realizado'
        grupo.loc[sem_datas, 'ORIGEM_PROJECAO_CORPORATIVA_REALISTA']   = 'realizado_sem_data'
        grupo.loc[sem_datas, 'ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO'] = 'realizado_sem_data'

        # Colunas CALCULADO sempre em branco para concluídas
        for col_vazia in [
            'INICIO_PROJECAO_CORPORATIVA_CALCULADO',
            'TERMINO_PROJECAO_CORPORATIVA_CALCULADO',
            'INICIO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
            'TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
        ]:
            if col_vazia not in grupo.columns:
                grupo[col_vazia] = pd.NaT
            grupo.loc[mask_concluida, col_vazia] = pd.NaT


        # Tarefas em aberto sem projeção lançada válida → recebem resultado calculado
        # Exclui: concluídas (tratadas acima) e resumos (herdados das filhas)
        lanc_dt   = pd.to_datetime(grupo['TERMINO_PROJECAO_LANCADO'], errors='coerce')
        # Máscara REALISTA: calcula quando sem lançado OU lançado no passado
        mask_calc_real = (
            ~mask_concluida &
            (is_sum_col == 0) &
            (
                grupo['TERMINO_PROJECAO_LANCADO'].isna() |
                (lanc_dt < hoje_proc)
            )
        )

        # Máscara COMPACTADO: só calcula quando sem lançado
        # Lançado no passado é mantido como meta no compactado
        mask_calc_comp = (
            ~mask_concluida &
            (is_sum_col == 0) &
            grupo['TERMINO_PROJECAO_LANCADO'].isna()
        )

        # ── Grava cenário REALISTA ────────────────────────────────────
        grupo.loc[mask_calc_real, 'INICIO_PROJECAO_CORPORATIVA_CALCULADO'] = \
            grupo.loc[mask_calc_real, '_ini_calc']
        grupo.loc[mask_calc_real, 'TERMINO_PROJECAO_CORPORATIVA_CALCULADO'] = \
            grupo.loc[mask_calc_real, '_fim_calc']
        grupo.loc[mask_calc_real, 'ORIGEM_PROJECAO_CORPORATIVA_REALISTA'] = \
            grupo.loc[mask_calc_real, '_origem_real']

        # ── Grava cenário COMPACTADO ──────────────────────────────────
        # Sem lançado: usa calculado compactado
        grupo.loc[mask_calc_comp, 'INICIO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO'] = \
            grupo.loc[mask_calc_comp, '_ini_comp']
        grupo.loc[mask_calc_comp, 'TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO'] = \
            grupo.loc[mask_calc_comp, '_fim_comp']
        grupo.loc[mask_calc_comp, 'ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO'] = \
            grupo.loc[mask_calc_comp, '_origem_comp']

        # Lançado no passado: compactado mantém o lançado como meta
        mask_lanc_passado = (
            ~mask_concluida &
            (is_sum_col == 0) &
            grupo['TERMINO_PROJECAO_LANCADO'].notna() &
            (lanc_dt < hoje_proc)
        )
        grupo.loc[mask_lanc_passado, 'INICIO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO'] = \
            pd.to_datetime(grupo.loc[mask_lanc_passado, 'INICIO_PROJECAO_LANCADO'], errors='coerce')
        grupo.loc[mask_lanc_passado, 'TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO'] = \
            pd.to_datetime(grupo.loc[mask_lanc_passado, 'TERMINO_PROJECAO_LANCADO'], errors='coerce')
        grupo.loc[mask_lanc_passado, 'ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO'] = 'lancado'

        mask_calc = mask_calc_real  # compatibilidade com código abaixo

        # ── Projeção lançada para tarefas em aberto com lançado válido ─
        # mask_lancado: tarefas com lançado futuro válido
        # Exclui tarefas que o forward_pass recalculou por predecessoras atrasadas
        # (_origem_real = 'calculado_atrasa' indica que a pred empurrou além do lançado)
        mask_lancado_base = (
            ~mask_concluida &
            (is_sum_col == 0) &
            grupo['TERMINO_PROJECAO_LANCADO'].notna() &
            (lanc_dt >= hoje_proc)
        )
        # Exclui do lancado as tarefas que o forward_pass marcou como atrasadas por predecessora
        mask_recalc_pred = mask_lancado_base & (grupo['_origem_real'] == 'calculado_atrasa')
        mask_lancado = mask_lancado_base & ~mask_recalc_pred

        # Tarefas com lançado mas predecessora atrasada → usa o calculado pelo forward_pass
        grupo.loc[mask_recalc_pred, 'INICIO_PROJECAO_CORPORATIVA'] = grupo.loc[mask_recalc_pred, '_ini_calc']
        grupo.loc[mask_recalc_pred, 'TERMINO_PROJECAO_CORPORATIVA'] = grupo.loc[mask_recalc_pred, '_fim_calc']
        grupo.loc[mask_recalc_pred, 'INICIO_PROJECAO_CORPORATIVA_CALCULADO'] = grupo.loc[mask_recalc_pred, '_ini_calc']
        grupo.loc[mask_recalc_pred, 'TERMINO_PROJECAO_CORPORATIVA_CALCULADO'] = grupo.loc[mask_recalc_pred, '_fim_calc']
        grupo.loc[mask_recalc_pred, 'ORIGEM_PROJECAO_CORPORATIVA_REALISTA'] = 'calculado_atrasa'

        # Tarefas com lançado válido e predecessoras OK → mantém lançado
        grupo.loc[mask_lancado, 'INICIO_PROJECAO_CORPORATIVA'] = \
            pd.to_datetime(grupo.loc[mask_lancado, 'INICIO_PROJECAO_LANCADO'], errors='coerce')
        grupo.loc[mask_lancado, 'TERMINO_PROJECAO_CORPORATIVA'] = \
            pd.to_datetime(grupo.loc[mask_lancado, 'TERMINO_PROJECAO_LANCADO'], errors='coerce')
        grupo.loc[mask_lancado, 'ORIGEM_PROJECAO_CORPORATIVA_REALISTA']  = 'lancado'
        grupo.loc[mask_lancado, 'ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO'] = 'lancado'

        # ── Projeção final para tarefas calculadas: usa o calculado ───
        grupo.loc[mask_calc, 'INICIO_PROJECAO_CORPORATIVA'] = \
            grupo.loc[mask_calc, 'INICIO_PROJECAO_CORPORATIVA_CALCULADO']
        grupo.loc[mask_calc, 'TERMINO_PROJECAO_CORPORATIVA'] = \
            grupo.loc[mask_calc, 'TERMINO_PROJECAO_CORPORATIVA_CALCULADO']

        # ── Resumos (tarefas macro) ───────────────────────────────────
        # Nunca recebem projeção própria no output — dependem das filhas.
        # As colunas INICIO/TERMINO_PROJECAO_CORPORATIVA ficam em branco.
        # _ini_calc/_fim_calc internos (min/max das filhas) são usados
        # apenas para propagação de predecessoras de outras tarefas.
        mask_resumo = (is_sum_col == 1) & ~mask_concluida
        for col_resumo in [
            'INICIO_PROJECAO_CORPORATIVA', 'TERMINO_PROJECAO_CORPORATIVA',
            'INICIO_PROJECAO_CORPORATIVA_CALCULADO', 'TERMINO_PROJECAO_CORPORATIVA_CALCULADO',
            'INICIO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
            'TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
            'ORIGEM_PROJECAO_CORPORATIVA_REALISTA', 'ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO',
        ]:
            if col_resumo not in grupo.columns:
                grupo[col_resumo] = pd.NaT if 'ORIGEM' not in col_resumo else ''
            if 'ORIGEM' in col_resumo:
                grupo.loc[mask_resumo, col_resumo] = 'resumo'
            else:
                grupo.loc[mask_resumo, col_resumo] = pd.NaT


        # ── Métricas por obra ─────────────────────────────────────────
        total      = len(grupo)
        calculadas = int(mask_calc.sum())
        concluidas = int(mask_concluida.sum())
        atrasam    = grupo['_atrasa'].sum()
        fim_real   = grupo.loc[~mask_concluida, '_fim_calc'].max()
        fim_comp   = grupo.loc[~mask_concluida, '_fim_comp'].max()

        atraso_real = atraso_comp = 0
        if pd.notna(data_fim_lb):
            if pd.notna(fim_real):
                atraso_real = max((fim_real - data_fim_lb).days, 0)
            if pd.notna(fim_comp):
                atraso_comp = max((fim_comp - data_fim_lb).days, 0)

        def status_label(dias):
            return ("🔴 ATRASA" if dias > 30
                    else "🟡 ATENÇÃO" if dias > 0
                    else "🟢 NO PRAZO")

        origens_real = grupo['_origem_real'].value_counts().to_dict()
        origens_comp = grupo['_origem_comp'].value_counts().to_dict()

        log(f"  {status_label(atraso_real)}  {projeto[:46]:<46}  "
            f"real={atraso_real:>4}d  comp={atraso_comp:>4}d  calc={calculadas}  concl={concluidas}/{total}")

        resumo_obras.append({
            'obra':                    projeto,
            'status_realista':         status_label(atraso_real),
            'status_compactado':       status_label(atraso_comp),
            'baseline_fim':            data_fim_lb.strftime('%d/%m/%Y') if pd.notna(data_fim_lb) else '',
            'projecao_fim_realista':   fim_real.strftime('%d/%m/%Y') if pd.notna(fim_real) else '',
            'projecao_fim_compactado': fim_comp.strftime('%d/%m/%Y') if pd.notna(fim_comp) else '',
            'atraso_dias_realista':    atraso_real,
            'atraso_dias_compactado':  atraso_comp,
            'tarefas_total':           total,
            'tarefas_calculadas':      calculadas,
            'tarefas_concluidas':      concluidas,
            'tarefas_atrasam':         int(atrasam),
            **{f'origem_real_{k}': v for k, v in origens_real.items()},
            **{f'origem_comp_{k}': v for k, v in origens_comp.items()},
        })

        # ── STATUS calculado pela lógica DAX do BI ──────────────────
        # Replica exatamente: Concluido / Nao Iniciado / Adiantado / Atrasado / Alerta / No Prazo
        # REALIZADO = TaskPercentWorkCompleted (ou TaskPercentCompleted)
        # PLANEJADO = DIAS_PCT_MES_ACUMULADO onde TIPO='LB'
        grupo['STATUS_CALCULADO_PROJECAO'] = grupo.apply(
            lambda r: calcular_status_curva(
                realizado       = pct_efetivo.get(r.name, 0) / 100.0,
                planejado       = float(r.get('DIAS_PCT_MES_ACUMULADO') or 0) / 100.0
                                  if r.get('TIPO', '') == 'LB' else 0.0,
                baseline_finish = r.get('TaskBaselineFinishDate') or r.get('Termino_LB'),
                anomes_ref      = r.get('ANOMES'),
            ), axis=1
        )

        grupo = grupo.drop(
            columns=['_ini_calc', '_fim_calc', '_ini_comp', '_fim_comp',
                     '_origem_real', '_origem_comp', '_atrasa'],
            errors='ignore'
        )
        resultados.append(grupo)

    df_final  = pd.concat(resultados, ignore_index=True)
    df_resumo = pd.DataFrame(resumo_obras)
    return df_final, df_resumo


# ──────────────────────────────────────────────
# 8. EXPORTAR
# ──────────────────────────────────────────────

def exportar(df_final, df_resumo, caminho_csv, caminho_xlsx):
    sep("EXPORTANDO")

    df_csv = df_final.copy()
    for col in df_csv.columns:
        if pd.api.types.is_datetime64_any_dtype(df_csv[col]):
            df_csv[col] = df_csv[col].dt.strftime('%Y-%m-%d')

    df_csv.to_csv(caminho_csv, index=False, encoding='utf-8-sig')
    log(f"✅ CSV exportado: {caminho_csv}")

    try:
        cols_saida = [
            'ProjectId', 'TaskId', 'ProjectName', 'TaskName',
            'TaskPercentCompleted', 'TaskIsSummary',
            # Projeção lançada (fonte correta)
            'INICIO_PROJECAO_LANCADO',
            'TERMINO_PROJECAO_LANCADO',
            # Cenário realista
            'INICIO_PROJECAO_CORPORATIVA_CALCULADO',
            'TERMINO_PROJECAO_CORPORATIVA_CALCULADO',
            'ORIGEM_PROJECAO_CORPORATIVA_REALISTA',
            # Cenário compactado
            'INICIO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
            'TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO',
            'ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO',
            # Consolidado final
            'INICIO_PROJECAO_CORPORATIVA',
            'TERMINO_PROJECAO_CORPORATIVA',
            # Baseline
            'Inicio_LB', 'Termino_LB',
        ]
        cols_ok = [c for c in cols_saida if c in df_final.columns]

        with pd.ExcelWriter(caminho_xlsx, engine='openpyxl') as writer:
            df_resumo.to_excel(writer, sheet_name='Resumo_Obras', index=False)
            df_final[cols_ok].to_excel(writer, sheet_name='Projecoes', index=False)

            # Aba só com calculadas — realista
            calc_real = df_final[
                df_final['TERMINO_PROJECAO_LANCADO'].isna() &
                df_final['TERMINO_PROJECAO_CORPORATIVA_CALCULADO'].notna()
            ][cols_ok]
            calc_real.to_excel(writer, sheet_name='Calculadas_Realista', index=False)

            # Aba só com calculadas — compactado
            calc_comp = df_final[
                df_final['TERMINO_PROJECAO_LANCADO'].isna() &
                df_final['TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO'].notna()
            ][cols_ok]
            calc_comp.to_excel(writer, sheet_name='Calculadas_Compactado', index=False)

        log(f"✅ Excel exportado: {caminho_xlsx}")
        log(f"   • Resumo_Obras            — status por obra (ambos cenários)")
        log(f"   • Projecoes               — todas as tarefas com projeção")
        log(f"   • Calculadas_Realista     — somente calculadas, cenário realista")
        log(f"   • Calculadas_Compactado   — somente calculadas, cenário compactado")

    except Exception as e:
        log(f"Erro Excel: {e}")


# ──────────────────────────────────────────────
# RESUMO FINAL
# ──────────────────────────────────────────────

def resumo_final(df_resumo, df_final):
    sep("RESUMO FINAL")

    total      = len(df_resumo)
    crit_real  = (df_resumo['atraso_dias_realista']   > 30).sum()
    atenc_real = ((df_resumo['atraso_dias_realista']  > 0) &
                  (df_resumo['atraso_dias_realista']  <= 30)).sum()
    prazo_real = (df_resumo['atraso_dias_realista']   == 0).sum()

    crit_comp  = (df_resumo['atraso_dias_compactado'] > 30).sum()
    atenc_comp = ((df_resumo['atraso_dias_compactado'] > 0) &
                  (df_resumo['atraso_dias_compactado'] <= 30)).sum()
    prazo_comp = (df_resumo['atraso_dias_compactado'] == 0).sum()

    calc  = df_final['TERMINO_PROJECAO_CORPORATIVA_CALCULADO'].notna().sum()
    lanc  = df_final['TERMINO_PROJECAO_LANCADO'].notna().sum()
    concl = (df_final.get('ORIGEM_PROJECAO_CORPORATIVA_REALISTA', pd.Series()) == 'realizado').sum()

    log(f"\n  Obras: {total} total")
    log(f"  Cenário REALISTA:    🔴 Críticas={crit_real}  🟡 Atenção={atenc_real}  🟢 No prazo={prazo_real}")
    log(f"  Cenário COMPACTADO:  🔴 Críticas={crit_comp}  🟡 Atenção={atenc_comp}  🟢 No prazo={prazo_comp}")
    log(f"  Tarefas: {len(df_final)} total | {concl} concluídas (sem cálculo) | {calc} calculadas | {lanc} lançadas mantidas")

    log("""
  Legenda de origens:
    realizado                  — tarefa concluída; repete Assignments sem cálculo
    lancado                    — tarefa em aberto com projeção lançada válida; mantida
    calculado                  — em aberto, calculado por velocidade + predecessoras, dentro do prazo
    calculado_atrasa           — calculado, ultrapassa o prazo da obra
    calculado_comprimido       — comprimido para caber no prazo (só cenário compactado)
    calculado_atraso_andamento — em andamento mas projeção caiu no passado; recalcula de hoje
    resumo_calculado           — tarefa resumo; herdou datas mín/máx das filhas
""")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Calcula PROJECAO_CORPORATIVA (realista + compactado) — EQTL'
    )
    parser.add_argument('--tasks',            default='EQUATORIAL_Tasks.csv')
    parser.add_argument('--curva',            default='CURVA_LB0.csv')
    parser.add_argument('--sep',              default=',',
                        help='Separador do CSV (padrão vírgula)')
    parser.add_argument('--saida',            default='projecao_calculada.csv')
    parser.add_argument('--saida_xlsx',       default='projecao_calculada.xlsx')
    parser.add_argument('--data_inicio',      default=None,
                        help='Processa tarefas a partir desta data (YYYY-MM-DD)')
    parser.add_argument('--data_fim',         default=None,
                        help='Processa tarefas até esta data (YYYY-MM-DD)')
    parser.add_argument('--fator_compressao', default=0.6, type=float,
                        help='Fator de compressão para cenário compactado (0-1, padrão=0.6)')
    args = parser.parse_args()

    print("\n" + "="*65)
    print("  CALCULO DE PROJECAO CORPORATIVA — EQTL")
    print("="*65)

    sep("1. LEITURA DOS DADOS")
    tasks_df = ler_csv(args.tasks, args.sep)
    curva_df = ler_csv(args.curva, args.sep)

    tasks_df = converter_datas(tasks_df, COLUNAS_DATA_TASKS)
    curva_df = converter_datas(curva_df, COLUNAS_DATA_CURVA)

    for col in ['TaskPercentCompleted', 'TaskPercentWorkCompleted',
                'TaskDuration', 'TaskBaselineDuration',
                'TaskOutlineLevel', 'TaskIndex', 'TaskIsSummary']:
        if col in tasks_df.columns:
            tasks_df[col] = pd.to_numeric(tasks_df[col], errors='coerce').fillna(0)

    log(f"\n  Tasks:  {len(tasks_df)} linhas | {tasks_df['ProjectName'].nunique()} obras")
    log(f"  Curva:  {len(curva_df)} linhas")

    # Filtro de período (novo)
    if args.data_inicio or args.data_fim:
        sep("1b. FILTRO DE PERÍODO")
        tasks_df = filtrar_por_periodo(tasks_df, args.data_inicio, args.data_fim)

    sem_proj = tasks_df['TERMINO_PROJECAO_LANCADO'].isna().sum()
    log(f"  Tarefas sem projeção lançada: {sem_proj}")

    sep("2. CALCULANDO VELOCIDADE DA CURVA S")
    vel_map = calcular_velocidade_curva(tasks_df, curva_df)
    log(f"  Velocidade calculada para {len(vel_map)} tarefas")
    log(f"  Velocidade média: {vel_map['velocidade_dia'].mean():.3f} %/dia")

    df_final, df_resumo = processar(tasks_df, vel_map, args.fator_compressao)
    exportar(df_final, df_resumo, args.saida, args.saida_xlsx)
    resumo_final(df_resumo, df_final)

    sep()
    log(f"\nConcluído!")
    log(f"  CSV:   {args.saida}")
    log(f"  Excel: {args.saida_xlsx}\n")


if __name__ == '__main__':
    main()