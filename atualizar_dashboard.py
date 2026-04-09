"""
╔══════════════════════════════════════════════════════════════════╗
║  ATUALIZAR DASHBOARD — EQTL                                     ║
║                                                                  ║
║  Lê o CSV gerado pelo projecao_corporativa.py e atualiza        ║
║  automaticamente o bloco de dados do index.html.                ║
║                                                                  ║
║  Como usar:                                                      ║
║  python atualizar_dashboard.py                                   ║
║      --csv   projecao_calculada.csv                             ║
║      --html  index.html                                          ║
║      --saida index.html   (pode ser o mesmo ou outro arquivo)   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import argparse
import json
import re
import warnings
warnings.filterwarnings('ignore')

import pandas as pd
from datetime import datetime

HORAS_POR_DIA = 8


def log(msg): print(msg)


def safe_float(v, default=0.0):
    try: return float(v or 0)
    except: return default

def safe_int(v, default=0):
    try: return int(float(v or 0))
    except: return default

def fmt_data(val):
    if pd.isna(val) or str(val).strip() in ('', 'nan', 'NaT', 'None'):
        return ''
    try:
        dt = pd.to_datetime(val, errors='coerce')
        return '' if pd.isna(dt) else dt.strftime('%d/%m/%Y')
    except:
        return ''

def fix_enc(s):
    """Corrige double-encoding latin-1/utf-8."""
    if not isinstance(s, str): return s
    try:
        return s.encode('latin-1').decode('utf-8')
    except:
        return s


# ──────────────────────────────────────────────
# 1. LEITURA DO CSV
# ──────────────────────────────────────────────

def ler_csv(caminho):
    log(f"  Lendo: {caminho}")
    for enc in ['utf-8-sig', 'utf-8', 'cp1252', 'latin-1']:
        try:
            df = pd.read_csv(caminho, encoding=enc, dtype=str,
                             low_memory=False, on_bad_lines='skip')
            df.columns = [c.strip() for c in df.columns]
            if 'ProjectName' not in df.columns or len(df) == 0:
                continue
            amostra = ' '.join(df['ProjectName'].dropna().head(3).tolist())
            # Detecta double-encoding
            if 'Ã' in amostra:
                log(f"  {enc}: double-encoding detectado, corrigindo...")
                for col in df.select_dtypes(include='object').columns:
                    df[col] = df[col].apply(fix_enc)
            log(f"  Encoding: {enc} | {len(df)} linhas | {df['ProjectName'].nunique()} obras")
            return df
        except Exception:
            continue
    raise RuntimeError(f"Não foi possível ler: {caminho}")


# ──────────────────────────────────────────────
# 2. CONSTRUIR OBJETO OBRAS
# ──────────────────────────────────────────────

def construir_obras(df):
    log("  Construindo dados das obras...")
    obras = {}
    hoje = pd.Timestamp.today().normalize()

    # Converte numéricos
    for col in ['TaskPercentCompleted', 'TaskPercentWorkCompleted', 'TaskIsSummary',
                'TaskIsCritical', 'TaskIndex', 'TaskOutlineLevel',
                'TaskBaselineDuration', 'TaskDuration']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    projetos = df['ProjectName'].unique()
    log(f"  Total de obras: {len(projetos)}")

    for projeto in projetos:
        grupo = df[df['ProjectName'] == projeto].copy()
        grupo = grupo.sort_values('TaskIndex').reset_index(drop=True)

        # ── Identifica resumos ────────────────────────────────────────────
        # TaskIsSummary pode estar zerado no CSV — usa ParentTaskId como fallback
        pais_ids = set(grupo['ParentTaskId'].dropna().unique()) if 'ParentTaskId' in grupo.columns else set()
        grupo['_is_resumo'] = (
            (grupo['TaskIsSummary'].astype(float) == 1) |
            (grupo['TaskId'].isin(pais_ids))
        )

        # ── pct efetivo: prioriza TaskPercentWorkCompleted ────────────────
        if 'TaskPercentWorkCompleted' in grupo.columns:
            grupo['_pct'] = grupo.apply(
                lambda r: float(r['TaskPercentWorkCompleted']) if float(r.get('TaskPercentWorkCompleted', 0) or 0) > 0
                else float(r.get('TaskPercentCompleted', 0) or 0), axis=1
            )
        else:
            grupo['_pct'] = grupo['TaskPercentCompleted'].astype(float)

        # ── Baseline e projeção da obra ───────────────────────────────────
        data_fim_lb_raw = pd.to_datetime(grupo['Termino_LB'], errors='coerce').dropna()
        data_fim_lb = data_fim_lb_raw.max() if len(data_fim_lb_raw) else pd.NaT
        data_fim_lb_fmt = fmt_data(data_fim_lb)

        # Projeção fim: usa coluna consolidada TERMINO_PROJECAO_CORPORATIVA
        proj_col = 'TERMINO_PROJECAO_CORPORATIVA'
        if proj_col in grupo.columns:
            proj_datas = pd.to_datetime(grupo[proj_col], errors='coerce').dropna()
            proj_fim_obra = fmt_data(proj_datas.max()) if len(proj_datas) else ''
        else:
            proj_fim_obra = ''

        # Atraso
        atraso_dias = 0
        if data_fim_lb_fmt and proj_fim_obra:
            try:
                dt_lb   = pd.to_datetime(data_fim_lb, errors='coerce')
                dt_proj = pd.to_datetime(proj_datas.max(), errors='coerce')
                if pd.notna(dt_lb) and pd.notna(dt_proj):
                    atraso_dias = max((dt_proj - dt_lb).days, 0)
            except:
                pass

        if atraso_dias > 30:
            status_obra = f"🔴 ATRASA — {atraso_dias} dias"
            badge_obra  = "badge-crit"
        elif atraso_dias > 0:
            status_obra = f"🟡 ATENÇÃO — {atraso_dias} dias"
            badge_obra  = "badge-att"
        else:
            status_obra = "🟢 NO PRAZO"
            badge_obra  = "badge-ok"

        sub = f"Baseline: {data_fim_lb_fmt} · Projeção: {proj_fim_obra}"

        # ── Constrói lista de tarefas ─────────────────────────────────────
        tarefas = []
        for _, row in grupo.iterrows():
            pct     = safe_int(row['_pct'])
            is_sum  = bool(row['_is_resumo'])
            is_crit = bool(float(row.get('TaskIsCritical', 0) or 0) == 1)

            # Origem realista
            o_real = str(row.get('ORIGEM_PROJECAO_CORPORATIVA_REALISTA', '') or '').strip()
            o_comp = str(row.get('ORIGEM_PROJECAO_CORPORATIVA_COMPACTADO', '') or '').strip()
            if is_sum:
                o_real = o_comp = 'resumo'
            elif pct >= 100:
                ini_r = row.get('Assignments_Inicio_Real_Ajustado', '') or row.get('TaskActualStartDate', '')
                fim_r = row.get('Assignments_Termino_Real_Ajustado', '') or row.get('TaskActualFinishDate', '')
                o_real = 'realizado' if (fmt_data(ini_r) and fmt_data(fim_r)) else 'realizado_sem_data'
                o_comp = o_real
            elif not o_real:
                fim_lanc = row.get('TERMINO_PROJECAO_LANCADO', '') or row.get('TERMINO_PROJECAO_CORPORATIVA_LANCADO', '')
                fim_calc = row.get('TERMINO_PROJECAO_CORPORATIVA_CALCULADO', '')
                if fmt_data(fim_lanc):
                    o_real = 'lancado'
                elif fmt_data(fim_calc):
                    o_real = 'calculado'
                else:
                    o_real = ''
                o_comp = o_comp or o_real

            # Status visual da tarefa (folha)
            if is_sum:
                st = 'resumo_pending'  # será herdado das filhas depois
            elif pct >= 100:
                st = 'concl'
            else:
                # Compara proj corporativa com Termino_LB da tarefa
                fim_proj_t = row.get('TERMINO_PROJECAO_CORPORATIVA', '')
                fim_lb_t   = row.get('Termino_LB', '')
                st = 'prazo'
                if fmt_data(fim_proj_t) and fmt_data(fim_lb_t):
                    try:
                        dt_p = pd.to_datetime(fim_proj_t, errors='coerce')
                        dt_l = pd.to_datetime(fim_lb_t, errors='coerce')
                        if pd.notna(dt_p) and pd.notna(dt_l):
                            delta = (dt_p - dt_l).days
                            if delta > 30: st = 'atrasada'
                            elif delta > 0: st = 'atencao'
                    except:
                        pass
                if st == 'prazo' and 'atrasa' in o_real.lower():
                    st = 'atrasada'

            # Datas
            ini_lb = '' if is_sum else fmt_data(row.get('Inicio_LB', ''))
            fim_lb = '' if is_sum else fmt_data(row.get('Termino_LB', ''))

            # Proj. realista
            if is_sum:
                proj_ini = proj_fim = ''
            elif pct >= 100:
                proj_ini = fmt_data(row.get('Assignments_Inicio_Real_Ajustado', '') or row.get('TaskActualStartDate', ''))
                proj_fim = fmt_data(row.get('Assignments_Termino_Real_Ajustado', '') or row.get('TaskActualFinishDate', ''))
            else:
                # REALISTA: usa coluna calculada realista
                # Prioridade: CALCULADO → LANCADO → CORPORATIVA (consolidada)
                proj_ini = (
                    fmt_data(row.get('INICIO_PROJECAO_CORPORATIVA_CALCULADO', '')) or
                    fmt_data(row.get('INICIO_PROJECAO_LANCADO', '')) or
                    fmt_data(row.get('INICIO_PROJECAO_CORPORATIVA', ''))
                )
                proj_fim = (
                    fmt_data(row.get('TERMINO_PROJECAO_CORPORATIVA_CALCULADO', '')) or
                    fmt_data(row.get('TERMINO_PROJECAO_LANCADO', '')) or
                    fmt_data(row.get('TERMINO_PROJECAO_CORPORATIVA', ''))
                )

            # Proj. compactado
            if is_sum:
                proj_ini_comp = proj_fim_comp = ''
            elif pct >= 100:
                proj_ini_comp, proj_fim_comp = proj_ini, proj_fim
            else:
                # COMPACTADO: usa coluna calculada compactado
                # Lançado no passado é mantido como meta → prioriza CALCULADO_COMPACTADO
                proj_ini_comp = (
                    fmt_data(row.get('INICIO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO', '')) or
                    fmt_data(row.get('INICIO_PROJECAO_LANCADO', '')) or
                    proj_ini
                )
                proj_fim_comp = (
                    fmt_data(row.get('TERMINO_PROJECAO_CORPORATIVA_CALCULADO_COMPACTADO', '')) or
                    fmt_data(row.get('TERMINO_PROJECAO_LANCADO', '')) or
                    proj_fim
                )

            tarefas.append({
                'nome':         str(row.get('TaskName', '') or '').strip(),
                'pct':          pct,
                'status':       st,
                'critica':      is_crit,
                'resumo':       is_sum,
                'ini_lb':       ini_lb,
                'fim_lb':       fim_lb,
                'proj_ini':     proj_ini,
                'proj_fim':     proj_fim,
                'origem':       o_real,
                'proj_fim_comp':  proj_fim_comp,
                'origem_comp':    o_comp,
                'proj_ini_comp':  proj_ini_comp,
            })

        # ── Herança de status: resumos herdam pior status das filhas ─────
        # Usa TaskId → ParentTaskId para herança correta (não apenas por posição)
        task_id_map = {}
        if 'TaskId' in grupo.columns:
            for i, row in grupo.iterrows():
                tid = str(row.get('TaskId', '') or '')
                if tid:
                    task_id_map[tid] = i

        # Mapeia índice do dataframe → índice da lista tarefas
        df_idx_to_list_idx = {df_i: list_i for list_i, (df_i, _) in enumerate(grupo.iterrows())}

        prioridade = {'atrasada': 4, 'atencao': 3, 'prazo': 2, 'concl': 1, 'resumo_pending': 0}

        # Processa de baixo para cima (filhas primeiro)
        for list_i in range(len(tarefas) - 1, -1, -1):
            t = tarefas[list_i]
            if t['status'] != 'resumo_pending':
                continue
            # Encontra filhas diretas via ParentTaskId
            df_i = list(grupo.iterrows())[list_i][0]
            meu_task_id = str(grupo.loc[df_i, 'TaskId'] if 'TaskId' in grupo.columns else '') or ''
            filhas_status = []
            if meu_task_id and 'ParentTaskId' in grupo.columns:
                filhas_mask = grupo['ParentTaskId'] == meu_task_id
                filhas_df_idx = grupo[filhas_mask].index.tolist()
                for fdi in filhas_df_idx:
                    fli = df_idx_to_list_idx.get(fdi)
                    if fli is not None and fli < len(tarefas):
                        fs = tarefas[fli]['status']
                        if fs != 'resumo_pending':
                            filhas_status.append(fs)
            # Fallback: posição sequencial (tarefas sem ParentTaskId)
            if not filhas_status:
                for j in range(list_i + 1, len(tarefas)):
                    if tarefas[j]['resumo']:
                        break
                    filhas_status.append(tarefas[j]['status'])
            if filhas_status:
                pior = max(filhas_status, key=lambda s: prioridade.get(s, 0))
                t['status'] = pior if pior != 'resumo_pending' else 'prazo'
            else:
                t['status'] = 'prazo'

        # ── KPIs ─────────────────────────────────────────────────────────
        folhas = [t for t in tarefas if not t['resumo']]
        total  = len(folhas)
        concl  = sum(1 for t in folhas if t['pct'] >= 100)
        atras  = sum(1 for t in folhas if t['status'] == 'atrasada')
        atenc  = sum(1 for t in folhas if t['status'] == 'atencao')
        prazo  = sum(1 for t in folhas if t['status'] == 'prazo' and t['pct'] < 100)
        crit   = sum(1 for t in folhas if t['critica'])
        calc   = sum(1 for t in folhas if t['origem'] and 'calculado' in t['origem'])

        obras[projeto] = {
            'status':  status_obra,
            'badge':   badge_obra,
            'sub':     sub,
            'kpis':    [total, concl, atras, atenc, prazo, crit, calc],
            'tarefas': tarefas,
        }
        icon = '✅' if badge_obra == 'badge-ok' else '🟡' if badge_obra == 'badge-att' else '🔴'
        log(f"  {icon} {projeto[:55]:<55} | {total} tarefas | atraso={atraso_dias}d | resumos={sum(1 for t in tarefas if t['resumo'])}")

    return obras


# ──────────────────────────────────────────────
# 3. ATUALIZAR HTML
# ──────────────────────────────────────────────

def atualizar_html(obras, caminho_html, caminho_saida):
    log(f"\n  Lendo HTML: {caminho_html}")
    with open(caminho_html, 'r', encoding='utf-8') as f:
        html = f.read()

    obras_json = json.dumps(obras, ensure_ascii=False, separators=(',', ':'))
    novo_bloco = f"const OBRAS={obras_json};"

    padrao = r'const OBRAS=\{.*?\};'
    if re.search(padrao, html, re.DOTALL):
        html = re.sub(padrao, novo_bloco, html, flags=re.DOTALL)
        log("  Bloco OBRAS substituído")
    else:
        raise RuntimeError("Padrão 'const OBRAS={...}' não encontrado no HTML.")

    total_obras   = len(obras)
    total_tarefas = sum(len(o['tarefas']) for o in obras.values())
    data_hoje     = datetime.today().strftime('%d/%m/%Y')

    html = re.sub(r'Atualizado: \d{2}/\d{2}/\d{4}', f'Atualizado: {data_hoje}', html)
    html = re.sub(r'\d+ obras? · \d+ tarefas?', f'{total_obras} obras · {total_tarefas} tarefas', html)

    with open(caminho_saida, 'w', encoding='utf-8') as f:
        f.write(html)

    log(f"  ✅ HTML salvo: {caminho_saida} | {data_hoje} | {total_obras} obras · {total_tarefas} tarefas")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Atualiza dashboard EQTL com dados do CSV')
    parser.add_argument('--csv',   default='projecao_calculada.csv')
    parser.add_argument('--html',  default='index.html')
    parser.add_argument('--saida', default=None)
    parser.add_argument('--obras', default=None, help='Filtrar obras (separadas por vírgula)')
    args = parser.parse_args()
    saida = args.saida or args.html

    print("\n" + "="*65)
    print("  ATUALIZAR DASHBOARD — EQTL")
    print("="*65)

    print("\n─── 1. LEITURA ──────────────────────────────────────────────")
    df = ler_csv(args.csv)
    for col in ['TaskPercentCompleted', 'TaskPercentWorkCompleted', 'TaskIsSummary',
                'TaskIsCritical', 'TaskIndex', 'TaskOutlineLevel']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    if args.obras:
        filtros = [o.strip() for o in args.obras.split(',')]
        df = df[df['ProjectName'].isin(filtros)]

    print("\n─── 2. CONSTRUINDO OBRAS ────────────────────────────────────")
    obras = construir_obras(df)

    print("\n─── 3. ATUALIZANDO HTML ─────────────────────────────────────")
    atualizar_html(obras, args.html, saida)

    print("\n" + "="*65)
    print(f"  ✅ Dashboard atualizado: {saida}")
    print(f"  📊 {len(obras)} obras processadas")
    print("="*65 + "\n")


if __name__ == '__main__':
    main()