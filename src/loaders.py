from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd

from .image_cache import IMAGE_EXTRACTED_AMOUNTS


@dataclass
class Dataset:
    profiles: pd.DataFrame
    events: pd.DataFrame
    messages: pd.DataFrame
    images: pd.DataFrame
    payment_options: pd.DataFrame
    requests: pd.DataFrame


def load_dataset(data_dir: str, requests_path: str = None) -> Dataset:
    profiles = pd.read_csv(os.path.join(data_dir, "financial_profiles.csv"))

    events = pd.read_csv(os.path.join(data_dir, "financial_events.csv"))
    # Fill blank amounts using the image-derived cache (never treat blank as 0).
    for eid, amt in IMAGE_EXTRACTED_AMOUNTS.items():
        events.loc[events["event_id"] == eid, "amount"] = amt
    events["event_date"] = pd.to_datetime(events["event_date"]).dt.date

    messages = pd.read_csv(os.path.join(data_dir, "messages.csv"))
    messages["sent_at_date"] = pd.to_datetime(messages["sent_at"]).dt.date

    images = pd.read_csv(os.path.join(data_dir, "images.csv"))

    payment_options = pd.read_csv(os.path.join(data_dir, "request_payment_options.csv"))
    payment_options["first_payment_date"] = pd.to_datetime(
        payment_options["first_payment_date"]).dt.date

    req_path = requests_path or os.path.join(data_dir, "requests.csv")
    requests = pd.read_csv(req_path)
    requests["request_date"] = pd.to_datetime(requests["request_date"]).dt.date
    requests["desired_completion_date"] = pd.to_datetime(
        requests["desired_completion_date"]).dt.date

    return Dataset(profiles=profiles, events=events, messages=messages,
                    images=images, payment_options=payment_options, requests=requests)
