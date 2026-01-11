{{ config(materialized='incremental') }}

with base as (
  select *
  from {{ ref('events_base.ff') }}
)
select
  event_id,
  updated_at,
  value
from base;