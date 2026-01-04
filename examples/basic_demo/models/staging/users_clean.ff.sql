{{ config(
    materialized='view',
    tags=[
        'example:basic_demo',
        'scope:staging',
        'engine:duckdb',
        'engine:postgres',
        'engine:databricks_spark',
        'engine:bigquery',
        'engine:snowflake_snowpark',
    ],
) }}

-- lineage: email_domain <- {{ source('crm','users') }}.email xform
-- lineage: signup_date  <- {{ source('crm','users') }}.signup_date

with raw_users as (
    select
        cast(id as integer) as user_id,
        lower(email) as email,
        cast(signup_date as date) as signup_date
    from {{ source('crm', 'users') }}
)

select
    user_id,
    email,
    regexp_replace(email, '^.*@', '') as email_domain,
    signup_date
from raw_users;
