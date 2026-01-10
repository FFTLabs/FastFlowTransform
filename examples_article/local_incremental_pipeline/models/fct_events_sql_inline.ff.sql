{{ config(
    materialized='incremental',
    unique_key='event_id',
    incremental={ 'updated_at_column': 'updated_at' },
) }}

with base as (
  select *
  from {{ ref('events_base.ff') }}
)
select
  event_id,
  updated_at,
  now() as written_at,
  value
from base
{% if is_incremental() %}
where updated_at > (
  select coalesce(max(updated_at), timestamp '1970-01-01 00:00:00')
  from {{ this }}
)
{% endif %};