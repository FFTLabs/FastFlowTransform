{{ config(materialized='table') }}

select
  event_id,
  cast(updated_at as timestamp) as updated_at,
  value
from {{ source('raw', 'events') }};