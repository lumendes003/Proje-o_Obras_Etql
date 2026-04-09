"""
╔══════════════════════════════════════════════════════════════════╗
║  EXTRAIR OBRAS DO SNOWFLAKE — EQTL                              ║
║                                                                  ║
║  Busca 3 obras por empresa que ainda NÃO estão finalizadas      ║
║  (obras onde a tarefa raiz tem TaskPercentCompleted < 100)      ║
║  e exporta os CSVs prontos para o projecao_corporativa.py.      ║
║                                                                  ║
║  Como usar:                                                      ║
║  python extrair_obras_snowflake.py                              ║
║      --account   seu_account.snowflakecomputing.com             ║
║      --user      seu_usuario                                    ║
║      --warehouse SEU_WAREHOUSE                                  ║
║      (login via SSO — abre o navegador automaticamente)         ║
║      --database  SB_OBRAS_AT                                    ║
║      --schema    EQTL_MA                                        ║
║      --obras_por_empresa  3                                     ║
║                                                                  ║
║  Ou configure as variáveis de ambiente:                         ║
║  SNOW_ACCOUNT, SNOW_USER, SNOW_PASSWORD, SNOW_WAREHOUSE         ║
╚══════════════════════════════════════════════════════════════════╝
"""

import argparse
import os
import warnings
warnings.filterwarnings('ignore')

import pandas as pd
import snowflake.connector
import yaml

SEP = "─" * 65

def log(msg): print(msg)
def sep(t=""): print(f"\n{SEP}\n  {t}\n{SEP}" if t else SEP)


# ──────────────────────────────────────────────
# 1. CONEXÃO SNOWFLAKE
# ──────────────────────────────────────────────

def ler_profiles(caminho, perfil='sb_obras_dbt', target=None):
    """Lê o profiles.yml do dbt e retorna as configurações de conexão."""
    with open(caminho, 'r', encoding='utf-8') as f:
        profiles = yaml.safe_load(f)

    if perfil not in profiles:
        raise RuntimeError(f"Perfil '{perfil}' não encontrado em {caminho}. "
                           f"Perfis disponíveis: {list(profiles.keys())}")

    cfg_perfil = profiles[perfil]
    target_name = target or cfg_perfil.get('target', 'dev')

    if target_name not in cfg_perfil.get('outputs', {}):
        raise RuntimeError(f"Target '{target_name}' não encontrado no perfil '{perfil}'.")

    cfg = cfg_perfil['outputs'][target_name]
    log(f"  Perfil: {perfil} → target: {target_name}")
    log(f"  Account:   {cfg.get('account')}")
    log(f"  User:      {cfg.get('user')}")
    log(f"  Warehouse: {cfg.get('warehouse')}")
    log(f"  Database:  {cfg.get('database')}")
    log(f"  Schema:    {cfg.get('schema')}")
    log(f"  Role:      {cfg.get('role', '—')}")
    return cfg


def conectar(args):
    # Se profiles.yml foi informado, usa ele
    # Resolve o profiles.yml na seguinte ordem:
    # 1. Argumento --profiles_yml (padrão: profiles.yml na pasta atual)
    # 2. Fallback: ~/.dbt/profiles.yml
    # 3. Último fallback: argumentos de linha de comando
    profiles_path = os.path.expanduser(args.profiles_yml) if args.profiles_yml else None

    if profiles_path and os.path.exists(profiles_path):
        log(f"  Usando profiles.yml: {profiles_path}")
        cfg = ler_profiles(profiles_path, args.perfil, args.target)
    else:
        padrao_dbt = os.path.expanduser('~/.dbt/profiles.yml')
        if os.path.exists(padrao_dbt):
            log(f"  profiles.yml local não encontrado. Usando: {padrao_dbt}")
            cfg = ler_profiles(padrao_dbt, args.perfil, args.target)
        else:
            log("  ⚠️  Nenhum profiles.yml encontrado. Usando argumentos de linha de comando.")
            cfg = {
                'account':       args.account,
                'user':          args.user,
                'warehouse':     args.warehouse,
                'database':      args.database,
                'schema':        args.schema,
                'authenticator': 'externalbrowser',
            }

    log("  Conectando ao Snowflake via SSO...")
    log("  ⚠️  O navegador será aberto para autenticação. Faça o login e volte aqui.")

    conn_params = {
        'account':       cfg.get('account', ''),
        'user':          cfg.get('user', ''),
        'warehouse':     cfg.get('warehouse', ''),
        'database':      args.database or cfg.get('database', ''),
        'schema':        args.schema   or cfg.get('schema', ''),
        'authenticator': cfg.get('authenticator', 'externalbrowser'),
    }
    # Role (opcional)
    if cfg.get('role'):
        conn_params['role'] = cfg['role']

    conn = snowflake.connector.connect(**conn_params)
    # Força database e schema na sessão — evita que o role sobrescreva
    cur = conn.cursor()
    cur.execute(f"USE DATABASE {conn_params['database']}")
    cur.execute(f"USE SCHEMA {conn_params['database']}.{conn_params['schema']}")
    cur.execute(f"USE WAREHOUSE {conn_params['warehouse']}")
    if conn_params.get('role'):
        cur.execute(f"USE ROLE {conn_params['role']}")
    cur.close()
    log(f"  ✅ Conectado: {conn_params['database']}.{conn_params['schema']}")
    return conn, conn_params['database'], conn_params['schema']


# ──────────────────────────────────────────────
# 2. BUSCAR OBRAS NÃO FINALIZADAS POR EMPRESA
# ──────────────────────────────────────────────

def buscar_obras(conn, n_obras, tabela_tasks):
    """
    Retorna até N obras por empresa que ainda não estão finalizadas.
    'Não finalizada' = obra onde a tarefa raiz (TaskOutlineLevel = 0)
    tem TaskPercentCompleted < 100.

    A empresa é extraída como o prefixo do ProjectName antes do primeiro '_'.
    Ex: 'EQTL MA_AMPLIAÇÃO SE BALSAS' → empresa = 'EQTL MA'
    """
    log(f"  Buscando {n_obras} obras por empresa não finalizadas...")

    sql_obras = f"""
WITH obras_raiz AS (
    SELECT
        "ProjectName"                                        AS project_name,
        SPLIT_PART("ProjectName", '_', 1)                   AS empresa,
        "TaskPercentCompleted"                               AS pct_concluido,
        MAX("Termino_LB")
            OVER (PARTITION BY "ProjectName")               AS data_fim_lb,
        ROW_NUMBER()
            OVER (
                PARTITION BY SPLIT_PART("ProjectName", '_', 1)
                ORDER BY
                    "TaskPercentCompleted" DESC NULLS LAST,
                    "ProjectName"
            )                                               AS rn_empresa
    FROM {tabela_tasks}
    WHERE "TaskOutlineLevel" = 0
      AND COALESCE("TaskPercentWorkCompleted", "TaskPercentCompleted", 0) < 100
)
SELECT
    project_name,
    empresa,
    pct_concluido,
    data_fim_lb,
    rn_empresa
FROM obras_raiz
WHERE rn_empresa <= {n_obras}
ORDER BY empresa, rn_empresa
;
"""
    cur = conn.cursor()
    cur.execute(sql_obras)
    df_obras = cur.fetch_pandas_all()
    cur.close()

    log(f"  Obras encontradas: {len(df_obras)}")
    empresas = df_obras['EMPRESA'].unique() if 'EMPRESA' in df_obras.columns else []
    for emp in empresas:
        obras_emp = df_obras[df_obras['EMPRESA'] == emp]['PROJECT_NAME'].tolist()
        log(f"    {emp}: {len(obras_emp)} obra(s)")
        for o in obras_emp:
            log(f"      • {o}")

    return df_obras['PROJECT_NAME'].tolist()


# ──────────────────────────────────────────────
# 3. EXTRAIR TASKS DAS OBRAS SELECIONADAS
# ──────────────────────────────────────────────

def extrair_tasks(conn, obras, tabela_tasks):
    """Extrai todas as tarefas das obras selecionadas."""
    log(f"\n  Extraindo tarefas de {len(obras)} obras...")

    # Monta lista segura para IN clause
    obras_str = ", ".join(f"'{o.replace(chr(39), chr(39)+chr(39))}'" for o in obras)

    sql = f"""
SELECT *
FROM {tabela_tasks}
WHERE "ProjectName" IN ({obras_str})
ORDER BY "ProjectName", TRY_TO_NUMBER("TaskIndex")
;
"""
    cur = conn.cursor()
    cur.execute(sql)
    df = cur.fetch_pandas_all()
    cur.close()

    log(f"  ✅ {len(df)} tarefas extraídas")
    return df


# ──────────────────────────────────────────────
# 4. EXTRAIR CURVA S
# ──────────────────────────────────────────────

def extrair_curva(conn, obras, tabela_curva, tabela_tasks, project_ids=None):
    """Extrai dados da Curva S (LB0) para as obras selecionadas."""
    log(f"  Extraindo Curva S...")

    if project_ids:
        ids_str = ", ".join(f"'{i}'" for i in project_ids)
        filtro  = f'"ProjectId" IN ({ids_str})'
    else:
        obras_str = ", ".join(f"'{o.replace(chr(39), chr(39)+chr(39))}'" for o in obras)
        filtro    = f'"ProjectId" IN (SELECT DISTINCT \"ProjectId\" FROM {tabela_tasks} WHERE \"ProjectName\" IN ({obras_str}))'

    sql = f"""
SELECT *
FROM {tabela_curva}
WHERE LB = 0          -- baseline LB0
  AND {filtro}
ORDER BY "ProjectId", "TaskId", TIPO, ANOMES
;
"""
    # Nota: traz todos os TIPOs do LB0 (incluindo planejado e realizado)
    # O projecao_corporativa.py usa TIPO='LB' como planejado
    # e os demais TIPOs como realizado para calcular o status DAX
    cur = conn.cursor()
    cur.execute(sql)
    df = cur.fetch_pandas_all()
    cur.close()

    log(f"  ✅ {len(df)} registros da Curva S extraídos")
    return df


# ──────────────────────────────────────────────
# 5. EXPORTAR CSVs
# ──────────────────────────────────────────────

def exportar(df_tasks, df_curva, saida_tasks, saida_curva):
    sep("EXPORTANDO")

    df_tasks.to_csv(saida_tasks, index=False, encoding='utf-8-sig')
    # Reescreve corrigindo encoding para compatibilidade
    df_tasks = pd.read_csv(saida_tasks, encoding='utf-8-sig', dtype=str, low_memory=False)
    df_tasks.to_csv(saida_tasks, index=False, encoding='utf-8-sig')
    log(f"  ✅ Tasks exportado: {saida_tasks}  ({len(df_tasks)} linhas)")

    df_curva.to_csv(saida_curva, index=False, encoding='utf-8-sig')
    log(f"  ✅ Curva S exportado: {saida_curva}  ({len(df_curva)} linhas)")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Extrai obras não finalizadas do Snowflake para o pipeline EQTL'
    )
    # Conexão via profiles.yml do dbt (recomendado)
    parser.add_argument('--profiles_yml', default='profiles.yml',
                        help='Caminho para o profiles.yml '
                             '(padrão: profiles.yml na pasta atual)')
    parser.add_argument('--perfil',  default='projecao_obras',
                        help='Nome do perfil no profiles.yml (padrão: sb_obras_dbt)')
    parser.add_argument('--target',  default=None,
                        help='Target do perfil (padrão: usa o target definido no perfil)')

    # Conexão manual (usado só se não houver profiles.yml)
    parser.add_argument('--account',   default=os.getenv('SNOW_ACCOUNT', ''),
                        help='Account Snowflake (ignorado se usar --profiles_yml)')
    parser.add_argument('--user',      default=os.getenv('SNOW_USER', ''),
                        help='Usuário Snowflake (ignorado se usar --profiles_yml)')
    parser.add_argument('--warehouse', default=os.getenv('SNOW_WAREHOUSE', ''),
                        help='Warehouse Snowflake (ignorado se usar --profiles_yml)')
    parser.add_argument('--database',  default=None,
                        help='Sobrescreve o database do perfil (opcional)')
    parser.add_argument('--schema',    default='EQTL_MA',
                        help='Sobrescreve o schema do perfil (opcional)')

    # Tabelas
    parser.add_argument('--tabela_tasks', default='"EQUATORIAL_Tasks"',
                        help='Nome da tabela de tasks')
    parser.add_argument('--tabela_curva', default='"MLPRO_CURVA_S_FISICA"',
                        help='Nome da tabela da Curva S')

    # Filtros
    parser.add_argument('--obras_por_empresa', type=int, default=3,
                        help='Quantidade de obras por empresa (padrão: 3)')
    parser.add_argument('--empresas', default=None,
                        help='Filtrar empresas específicas separadas por vírgula '
                             '(ex: "EQTL MA,EQTL PA"). Padrão: todas.')

    # Saída
    parser.add_argument('--saida_tasks', default='EQUATORIAL_Tasks.csv',
                        help='Arquivo CSV de saída das tasks')
    parser.add_argument('--saida_curva', default='CURVA_LB0.csv',
                        help='Arquivo CSV de saída da Curva S')

    args = parser.parse_args()

    print("\n" + "="*65)
    print("  EXTRAIR OBRAS SNOWFLAKE — EQTL")
    print("="*65)

    sep("1. CONEXÃO")
    conn, database, schema = conectar(args)
    args.database = database
    args.schema   = schema

    # Monta nome completo das tabelas APÓS conexão (database/schema resolvidos do profiles.yml)
    tabela_tasks = f'{args.database}.{args.schema}.{args.tabela_tasks}'
    tabela_curva = f'{args.database}.{args.schema}.{args.tabela_curva}'
    log(f"  Tabela tasks: {tabela_tasks}")
    log(f"  Tabela curva: {tabela_curva}")

    sep("2. SELEÇÃO DE OBRAS")

    # Filtro de empresas
    if args.empresas:
        empresas_filtro = [e.strip() for e in args.empresas.split(',')]
        log(f"  Filtro de empresas: {empresas_filtro}")

        # Adiciona WHERE na query de obras
        import snowflake.connector
        emp_str = ", ".join(f"'{e}'" for e in empresas_filtro)
        obras = buscar_obras_filtradas(conn, args.obras_por_empresa,
                                       tabela_tasks, emp_str)
    else:
        obras = buscar_obras(conn, args.obras_por_empresa, tabela_tasks)

    if not obras:
        log("\n  ⚠️  Nenhuma obra encontrada com os critérios informados.")
        conn.close()
        return

    sep("3. EXTRAÇÃO DE DADOS")
    df_tasks = extrair_tasks(conn, obras, tabela_tasks)
    # Pega ProjectIds únicos das tasks já extraídas
    project_ids = df_tasks["ProjectId"].dropna().unique().tolist() if "ProjectId" in df_tasks.columns else []
    df_curva  = extrair_curva(conn, obras, tabela_curva, tabela_tasks, project_ids)

    conn.close()
    log("  Conexão encerrada.")

    exportar(df_tasks, df_curva, args.saida_tasks, args.saida_curva)

    sep("PRÓXIMOS PASSOS")
    log(f"""
  Agora rode o pipeline completo:

  1. Calcular projeções:
     python projecao_corporativa.py \\
         --tasks {args.saida_tasks} \\
         --curva {args.saida_curva}

  2. Atualizar dashboard:
     python atualizar_dashboard.py \\
         --csv  projecao_calculada.csv \\
         --html index.html \\
         --saida index_novo.html
""")
    sep()


def buscar_obras_filtradas(conn, n_obras, tabela_tasks, emp_str):
    """Versão com filtro de empresas específicas."""
    sql_obras = f"""
WITH obras_raiz AS (
    SELECT
        "ProjectName"                                        AS project_name,
        SPLIT_PART("ProjectName", '_', 1)                   AS empresa,
        "TaskPercentCompleted"                               AS pct_concluido,
        ROW_NUMBER()
            OVER (
                PARTITION BY SPLIT_PART("ProjectName", '_', 1)
                ORDER BY "TaskPercentCompleted" DESC NULLS LAST, "ProjectName"
            )                                               AS rn_empresa
    FROM {tabela_tasks}
    WHERE "TaskOutlineLevel" = 0
      AND COALESCE("TaskPercentWorkCompleted", "TaskPercentCompleted", 0) < 100
      AND SPLIT_PART("ProjectName", '_', 1) IN ({emp_str})
)
SELECT project_name, empresa, pct_concluido, rn_empresa
FROM obras_raiz
WHERE rn_empresa <= {n_obras}
ORDER BY empresa, rn_empresa
;
"""
    cur = conn.cursor()
    cur.execute(sql_obras)
    df = cur.fetch_pandas_all()
    cur.close()

    log(f"  Obras encontradas: {len(df)}")
    for _, row in df.iterrows():
        log(f"    • {row.get('PROJECT_NAME', row.iloc[0])}")

    col = 'PROJECT_NAME' if 'PROJECT_NAME' in df.columns else df.columns[0]
    return df[col].tolist()


if __name__ == '__main__':
    main()