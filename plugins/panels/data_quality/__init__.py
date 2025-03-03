"""
Data quality panel.

| Copyright 2017-2025, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import uuid
import bson

import fiftyone.operators as foo
from fiftyone.operators import Panel, PanelConfig
from fiftyone.operators.delegated import DelegatedOperationService
import fiftyone.operators.types as types
from fiftyone import ViewField as F

from .constants import (
    ISSUE_TYPES,
    DEFAULT_ISSUE_CONFIG,
    DEFAULT_ISSUE_COUNTS,
    FIELD_NAME,
    TITLE,
    STATUS,
    STATUS_COLOR,
    DEFAULT_COMPUTING,
    SAMPLE_STORE,
    IMAGES,
    LAST_SCAN,
)


BRIGHTNESS_OPERATOR = "@voxel51/panels/compute_brightness"
BLURRINESS_OPERATOR = "@voxel51/panels/compute_blurriness"
ASPECT_RATIO_OPERATOR = "@voxel51/panels/compute_aspect_ratio"
ENTROPY_OPERATOR = "@voxel51/panels/compute_entropy"
NEAR_DUPLICATES_OPERATOR = "@voxel51/panels/compute_near_duplicates"
EXACT_DUPLICATES_OPERATOR = "@voxel51/panels/compute_exact_duplicates"

ICON_PATH = "troubleshoot"
NOT_PERMITTED_TEXT = "You do not have sufficient permission."
operator_mapping = {
    "brightness": BRIGHTNESS_OPERATOR,
    "blurriness": BLURRINESS_OPERATOR,
    "aspect_ratio": ASPECT_RATIO_OPERATOR,
    "entropy": ENTROPY_OPERATOR,
    "near_duplicates": NEAR_DUPLICATES_OPERATOR,
    "exact_duplicates": EXACT_DUPLICATES_OPERATOR,
}


def missing_min_access_required(ctx, min_required_dataset_access="EDIT"):
    if ctx.user is None:
        return False

    user_dataset_access = ctx.user.dataset_permission

    if min_required_dataset_access == "TAG":
        return user_dataset_access not in ["TAG", "EDIT", "MANAGE"]
    if min_required_dataset_access == "EDIT":
        return user_dataset_access not in ["EDIT", "MANAGE"]
    return True


class DataQualityPanel(Panel):
    version = "v1"

    @property
    def config(self):
        return PanelConfig(
            name="data_quality_panel",
            label="Data Quality",
            category=foo.Categories.CURATE,
            beta=True,
            icon=ICON_PATH,
            light_icon=ICON_PATH,
            dark_icon=ICON_PATH,
        )

    ###
    # STORE METHODS
    ###

    def _get_store_key(self, ctx):
        return (
            "data_quality_store_"
            + str(ctx.dataset._dataset._doc.id)
            + self.version
        )

    def get_store(self, ctx):
        """Returns latest state of execution store or creates a new one with
        given key.
        """
        return ctx.store(self._get_store_key(ctx))

    ###
    # LIFECYCLE METHODS
    ###

    def on_load(self, ctx):
        """Set initial state"""

        # generic state
        ctx.panel.state.screen = "home"
        ctx.panel.state.issue_type = None
        ctx.panel.state.alert = ""
        ctx.panel.state.tags = []
        ctx.panel.state.first_open = False
        ctx.panel.state.dataset_name = ctx.dataset.name
        ctx.panel.state.new_sample_scan = True

        # load store
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)
        if content is None:
            ctx.panel.state.first_open = True

            store.set(key, SAMPLE_STORE)

            ctx.panel.state.issue_config = DEFAULT_ISSUE_CONFIG
            ctx.panel.state.issue_counts = DEFAULT_ISSUE_COUNTS
            ctx.panel.state.computing = DEFAULT_COMPUTING
            ctx.panel.state.last_scan = LAST_SCAN

            results = SAMPLE_STORE["results"]
        else:
            ctx.panel.state.issue_config = content.get(
                "config", DEFAULT_ISSUE_CONFIG
            )
            ctx.panel.state.issue_counts = content.get(
                "counts", DEFAULT_ISSUE_COUNTS
            )
            ctx.panel.state.computing = content.get(
                "computing", DEFAULT_COMPUTING
            )
            ctx.panel.state.last_scan = content.get("last_scan", LAST_SCAN)

            results = content.get("results", SAMPLE_STORE.get("results"))

        # format: [new_samples_count, check ran, rescan completed]
        ctx.panel.state.new_samples = {
            "brightness": [0, False, False],
            "blurriness": [0, False, False],
            "aspect_ratio": [0, False, False],
            "entropy": [0, False, False],
            "near_duplicates": [0, False, False],
            "exact_duplicates": [0, False, False],
        }

    def on_change_selected(self, ctx):
        if (
            ctx.panel.state.issue_type == "exact_duplicates"
            and ctx.panel.state.screen == "analysis"
        ):
            self.toggle_select_from_grid(ctx)

    def on_unload(self, ctx):
        if ctx.dataset is None:
            return

        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        if content is not None and ctx.panel.state.computing is not None:
            content["computing"] = ctx.panel.state.computing
            store.set(key, content)

        ctx.ops.clear_view()

    ###
    # EVENT HANDLERS
    ###

    def should_reset_issue(self, ctx, issue_type):
        """Determines whether an issue's store values need to be reset."""

        # only check issues with status > computing
        if self._get_issue_status(ctx, issue_type) in [STATUS[2], STATUS[3]]:
            sample_count_with_field = len(
                ctx.dataset.exists(FIELD_NAME[issue_type], bool=True)
            )

            if sample_count_with_field == 0:
                ctx.log(f"should_reset_issue:{issue_type}")
                return True

        return False

    def reset_issue(self, ctx, issue_type):
        # change status
        self.change_computing_status(
            ctx,
            issue_type,
            is_computing=False,
            issue_status=STATUS[0],
        )

        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        # clear old data
        content["results"][issue_type] = SAMPLE_STORE["results"][issue_type]
        content["last_scan"][issue_type] = SAMPLE_STORE["last_scan"][
            issue_type
        ]

        store.set(key, content)

    def navigate_to_screen(
        self, ctx, issue_type=None, next_screen="home", recompute=False
    ):
        _issue_type = ctx.params.get("issue_type", issue_type)
        issue_type = _issue_type if _issue_type is not None else issue_type

        _next_screen = ctx.params.get("next_screen", next_screen)
        next_screen = _next_screen if _next_screen is not None else next_screen

        _recompute = ctx.params.get("recompute", recompute)
        recompute = _recompute if _recompute is not None else recompute

        ctx.panel.state.issue_type = issue_type
        ctx.panel.state.screen = next_screen
        ctx.panel.state.first_open = False

        # rest any saved values
        if next_screen == "home":
            # Reset the view
            ctx.panel.state.hist_lower_thresh = None
            ctx.panel.state.hist_upper_thresh = None
            ctx.panel.state.issue_type = None
            ctx.ops.set_view(ctx.dataset.view())

        # when change to analysis screen, change view
        if next_screen == "analysis":
            try:
                if recompute:
                    self._process_issue_computation(ctx, issue_type, recompute)

                if issue_type in [
                    "brightness",
                    "blurriness",
                    "aspect_ratio",
                    "entropy",
                    "near_duplicates",
                ]:
                    self.set_hist_defaults(ctx)

                if (
                    self._get_issue_status(ctx, issue_type) != STATUS[3]
                ):  # change any non-reviewed statuses
                    self.change_computing_status(
                        ctx, issue_type, issue_status=STATUS[2]
                    )

            except ValueError as e:
                ctx.log(f"navigate_to_screen warning: {e}")
                if self.should_reset_issue(ctx, issue_type):
                    self.reset_issue(ctx, issue_type)
                    ctx.panel.state.screen = "pre_load_compute"
                    return
                else:
                    raise e

            self.change_view(ctx, issue_type)

    def _on_compute_option_selected(
        self, ctx, execution_option="execute", run_id=""
    ):
        issue_type = ctx.params.get("issue_type", ctx.panel.state.issue_type)
        dropdown_value = ctx.params.get("selected_option", {}).get(
            "id", execution_option
        )

        new_samples = ctx.panel.state.new_samples

        # new samples > 0 so we are within rescan logic
        if new_samples[issue_type][0] > 0:
            new_samples[issue_type] = [
                new_samples[issue_type][0],
                new_samples[issue_type][1],
                True,
            ]
            ctx.panel.state.set("new_samples", new_samples)

        if dropdown_value == "execute":
            self.change_computing_status(
                ctx,
                issue_type,
                is_computing=True,
                execution_type="execute",
                delegation_run_id="",
                delegation_status="",
                issue_status=STATUS[1],
            )
        elif dropdown_value in ["delegate", "delegate_execution"]:
            self.change_computing_status(
                ctx,
                issue_type,
                is_computing=True,
                execution_type="delegate_execution",
                delegation_run_id=str(run_id),
                delegation_status="",
                issue_status=STATUS[1],
            )

    def on_change_set_threshold(self, ctx):
        issue_type = ctx.panel.state.issue_type

        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        config = content.get("config", {})
        count = content.get("counts", DEFAULT_ISSUE_COUNTS)
        current_counts = content.get("current_counts", DEFAULT_ISSUE_COUNTS)

        if ctx.params["value"] == "Save Threshold":
            config[issue_type]["min"] = ctx.panel.state.hist_lower_thresh
            config[issue_type]["max"] = ctx.panel.state.hist_upper_thresh
            count[issue_type] = current_counts[issue_type]
            ctx.panel.state.issue_config = config

            store.set(key, content)

            ctx.panel.state.set(
                f"header_{issue_type}.collapsed_sub_left_{issue_type}.issue_count_{issue_type}_home_page",
                current_counts[issue_type],
            )
        elif ctx.params["value"] == "Reset Threshold":
            # NOTE: the order of calls matter
            config[issue_type] = DEFAULT_ISSUE_CONFIG[issue_type]
            ctx.panel.state.issue_config = config

            default_lower, default_upper = self.get_plot_defaults(
                ctx, issue_type, config[issue_type]["detect_method"]
            )

            ctx.panel.state.hist_lower_thresh = default_lower
            ctx.panel.state.hist_upper_thresh = default_upper

            # reset counts for home page based on default threshold
            field = FIELD_NAME[issue_type]
            view = ctx.dataset.match(
                (F(field) >= default_lower) & (F(field) <= default_upper)
            )
            num_issue_samples = len(view)
            count[issue_type] = num_issue_samples

            store.set(key, content)

            # reset the slider's state
            ctx.panel.state.set(
                f"{issue_type}_analysis.{issue_type}_analysis_content.double_slider_{issue_type}",
                [default_lower, default_upper],
            )

            self.change_view(ctx, issue_type)

    def change_view(self, ctx, issue_type):
        """Updates the view based on the histogram bounds."""
        field = FIELD_NAME[issue_type]

        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        counts = content.get("counts", DEFAULT_ISSUE_COUNTS)
        current_counts = content.get("current_counts", DEFAULT_ISSUE_COUNTS)

        # If not started, update the status here
        if content["status"][issue_type] == STATUS[0]:
            content["status"][issue_type] = STATUS[2]

        if issue_type not in ("near_duplicates", "exact_duplicates") and (
            ctx.panel.state.hist_lower_thresh
            or ctx.panel.state.hist_upper_thresh
        ):
            view = ctx.dataset.match(
                (F(field) >= ctx.panel.state.hist_lower_thresh)
                & (F(field) <= ctx.panel.state.hist_upper_thresh)
            )
            num_issue_samples = len(view)

            ctx.ops.set_view(view)
            ctx.panel.state.issue_counts[issue_type] = num_issue_samples

            current_counts[issue_type] = num_issue_samples

            store.set(key, content)

            ctx.panel.state.set(
                f"{issue_type}_analysis.header_{issue_type}.collaspsed_sub_left_{issue_type}.issue_count_{issue_type}_analysis_page",
                num_issue_samples,
            )

            return

        if issue_type == "near_duplicates" and (
            ctx.panel.state.hist_lower_thresh
            or ctx.panel.state.hist_upper_thresh
        ):
            view = ctx.dataset.match(
                (F(field) >= ctx.panel.state.hist_lower_thresh)
                & (F(field) <= ctx.panel.state.hist_upper_thresh)
            )
            num_issue_samples = len(view)

            ctx.panel.state.issue_counts[issue_type] = num_issue_samples
            content["current_counts"][issue_type] = num_issue_samples
            store.set(key, content)

            ctx.panel.state.set(
                f"{issue_type}_analysis.header_{issue_type}.collaspsed_sub_left_{issue_type}.issue_count_{issue_type}_analysis_page",
                num_issue_samples,
            )

            ctx.ops.set_view(view)
            return

        if issue_type == "exact_duplicates" and ctx.dataset.has_field(field):
            dup_filehashes = content["results"][issue_type]["dup_filehash"]
            num_duplicates = content["counts"][issue_type]

            exact_dup_view = ctx.dataset.match(
                F("filehash").is_in(dup_filehashes)
            )
            exact_dup_view = exact_dup_view.sort_by(
                "filehash", create_index=False
            )

            ctx.panel.state.issue_counts[issue_type] = num_duplicates
            counts[issue_type] = num_duplicates
            current_counts[issue_type] = num_duplicates
            store.set(key, content)

            ctx.ops.set_view(exact_dup_view)
            return

        ctx.ops.clear_view()
        return

    def get_threshold_range(
        self, min_v, max_v, lower_std_bound, upper_std_bound
    ):
        if (
            lower_std_bound <= min_v <= upper_std_bound
            and lower_std_bound <= max_v <= upper_std_bound
        ):
            return min_v, max_v

        if min_v < lower_std_bound and max_v <= upper_std_bound:
            return min_v, lower_std_bound

        if max_v > upper_std_bound and min_v >= lower_std_bound:
            return upper_std_bound, max_v

        if min_v < lower_std_bound and max_v > upper_std_bound:
            if (lower_std_bound - min_v) > (max_v - upper_std_bound):
                return min_v, lower_std_bound
            else:
                return upper_std_bound, max_v

        return lower_std_bound, upper_std_bound

    def get_plot_defaults(self, ctx, field, method):
        """Set the histogram thresholds based on selected issue type."""
        issue_config = ctx.panel.state.issue_config[field]

        new_lower = issue_config["min"] if "min" in issue_config else None
        new_upper = issue_config["max"] if "max" in issue_config else None

        min_v, max_v = ctx.dataset.bounds(FIELD_NAME[field])

        if (
            new_lower
            and new_upper
            and new_lower >= min_v
            and new_lower < max_v
            and new_upper > min_v
            and new_upper <= max_v
        ):
            return new_lower, new_upper

        if method == "percentage":
            value_range = max_v - min_v

            # Calculate lower_thresh based on percentage, or fallback to min_v
            lower_factor = new_lower * value_range
            lower_thresh = (
                min_v + lower_factor
                if new_lower is not None and (min_v + lower_factor) <= max_v
                else min_v
            )

            # Calculate upper_thresh based on percentage, or fallback to min_v
            upper_factor = new_upper * value_range
            upper_thresh = (
                min_v + upper_factor
                if new_upper is not None and (min_v + upper_factor) >= min_v
                else max_v
            )

            if upper_thresh < lower_thresh:
                lower_thresh = upper_thresh = min_v

            return lower_thresh, upper_thresh

        elif method == "threshold":
            if new_upper < min_v:
                # threshold max is lower than bound's min value
                return new_upper, new_upper

            if new_lower > max_v:
                # threshold min is larger than bound's max value
                return new_lower, new_lower

            return new_lower, new_upper

    def set_hist_defaults(self, ctx):
        """Set the histogram thresholds based on selected issue type"""
        field = ctx.panel.state.issue_type
        issue_config = ctx.panel.state.issue_config[field]

        lower_threshold = ctx.panel.state.hist_lower_thresh
        upper_threshold = ctx.panel.state.hist_upper_thresh

        if lower_threshold is None or upper_threshold is None:
            lower_threshold, upper_threshold = self.get_plot_defaults(
                ctx, field, issue_config["detect_method"]
            )

            ctx.panel.state.hist_lower_thresh = lower_threshold
            ctx.panel.state.hist_upper_thresh = upper_threshold

            # sets slider's defaultValue
            ctx.panel.state.set(
                f"{field}_analysis.{field}_analysis_content.double_slider_{field}",
                [lower_threshold, upper_threshold],
            )

    def prepare_histogram_data(
        self, counts, edges, lower_thresh, upper_thresh
    ):
        in_threshold_counts = []
        out_of_threshold_counts = []

        # Iterate over counts and edges
        for i in range(len(counts)):
            range_start = edges[i]
            range_end = edges[i + 1]  # the end of the current range
            midpoint = (range_start + range_end) / 2  # midpoint of the range

            # Compare the midpoint to the threshold ranges
            if lower_thresh <= midpoint <= upper_thresh:
                # The midpoint is within the threshold range
                in_threshold_counts.append(counts[i])
                out_of_threshold_counts.append(0)
            else:
                # The midpoint is outside the threshold range
                in_threshold_counts.append(0)
                out_of_threshold_counts.append(counts[i])

        return in_threshold_counts, out_of_threshold_counts

    def slider_change(self, ctx):
        """Update the selected thresholds (but not the range min/max)."""
        ctx.panel.state.hist_lower_thresh = ctx.params["value"][0]
        ctx.panel.state.hist_upper_thresh = ctx.params["value"][1]

        # Change view based on new thresholds
        self.change_view(ctx, ctx.panel.state.issue_type)

    def toggle_select(self, ctx):
        ctx.ops.set_selected_samples(ctx.params["value"])

    def toggle_select_from_grid(self, ctx):
        ctx.panel.state.set(
            "exact_duplicates_analysis.exact_duplicates_analysis_content.exact_duplicate_selections",
            ctx.selected,
        )

    def _get_tag_helper_text(self, ctx):
        if len(ctx.selected) == 0:
            return f"Tag {self._get_current_issue_count(ctx, ctx.panel.state.issue_type)} samples in current view:"

        if self._get_current_issue_count(ctx, ctx.panel.state.issue_type) == 0:
            return f"Tag {len(ctx.selected)} samples currently selected:"

        return f"Tag {len(ctx.selected)} out of {self._get_current_issue_count(ctx, ctx.panel.state.issue_type)} samples currently selected:"

    def _get_issue_count(self, ctx, issue_type):
        """Get the status of the issue type (from execution store)"""
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        if content is None:
            return 0

        return content.get("counts", {}).get(issue_type, 0)

    def _get_current_issue_count(self, ctx, issue_type):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        if content is None:
            return 0

        if content["current_counts"][issue_type] is None:
            return content["counts"][issue_type] or 0

        return content["current_counts"][issue_type] or 0

    def _get_issue_status(self, ctx, issue_type):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        if content is None:
            return STATUS[0]

        return content.get("status", {}).get(issue_type, STATUS[0])

    def _change_issue_status(self, ctx, issue_type=None, new_status=None):
        """Change the status of the issue type"""
        if issue_type is None:
            issue_type = ctx.panel.state.issue_type
        if new_status is not None:
            status = new_status
        else:
            status = ctx.params.get("value", "")

        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)
        content["status"][issue_type] = status
        store.set(key, content)

        if status == STATUS[3]:
            ctx.panel.state.alert = "reviewed"
        if status == STATUS[2]:
            ctx.panel.state.alert = "in_review"

    def mark_as_reviewed(self, ctx):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)
        content["status"][ctx.panel.state.issue_type] = STATUS[3]
        store.set(key, content)

        ctx.panel.state.alert = "reviewed"

    def mark_as_reviewed_in_modal(self, ctx):
        self.mark_as_reviewed(ctx)
        self.navigate_to_screen(ctx, next_screen="home")

    def _tag_samples(self, ctx):
        tags = ctx.params.get("tags", ctx.panel.state.tags)

        if ctx.selected:
            target_view = ctx.dataset.select(ctx.selected)
        else:
            target_view = ctx.target_view()

        target_view.tag_samples(tags)

        ctx.panel.state.tags = tags
        ctx.panel.state.alert = "tagging"

    def estimate_execution_wait_time(self, ctx, subset=0):
        """Estimate the wait time in seconds for the next execution."""
        if subset > 0:
            dataset_size = subset
        else:
            dataset_size = ctx.dataset.count()

        if ctx.panel.state.issue_type == "brightness":
            return 45 * (dataset_size // 5000)

        if ctx.panel.state.issue_type == "blurriness":
            return 45 * (dataset_size // 5000)

        if ctx.panel.state.issue_type == "aspect_ratio":
            return 45 * (dataset_size // 5000)

        if ctx.panel.state.issue_type == "entropy":
            return 45 * (dataset_size // 5000)

        if ctx.panel.state.issue_type == "near_duplicates":
            return 45 * 3 * (dataset_size // 5000)

        if ctx.panel.state.issue_type == "exact_duplicates":
            return 45 * 2 * (dataset_size // 5000)

        return 0

    def check_for_new_samples(self, ctx):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)
        # Retrieve previous results from the store
        results = content.get("results", SAMPLE_STORE["results"])

        # Get last scan times for each issue type
        last_scan_times = {
            issue_type: (
                (ctx.panel.state.last_scan.get(issue_type) or {}).get(
                    "timestamp"
                )
                or (LAST_SCAN.get(issue_type) or {}).get("timestamp")
            )
            for issue_type in ISSUE_TYPES
        }

        # Loop over all issue types
        for issue_type in ISSUE_TYPES:
            # For non-"exact_duplicates", ensure that previous results have valid counts and edges
            if issue_type != "exact_duplicates":
                prev_res = results.get(issue_type, {})
                if (
                    prev_res.get("counts") is None
                    or prev_res.get("edges") is None
                ):
                    # Skip this issue type if results are incomplete
                    continue

            # Skip if the check for new samples has already been run for this issue type
            if ctx.panel.state.new_samples[issue_type][1]:
                continue

            last_scan = last_scan_times.get(issue_type) or (
                datetime.utcnow() + timedelta(days=1)
            )
            last_modified = ctx.dataset._max("last_modified_at") or last_scan

            if last_modified <= last_scan:
                ctx.panel.state.new_samples[issue_type] = [0, True, False]
                continue

            field_name = FIELD_NAME[issue_type]
            new_samples_view = ctx.dataset.exists(field_name, bool=False)

            if len(new_samples_view) > 0:
                ctx.panel.state.new_samples[issue_type] = [
                    len(new_samples_view),
                    True,
                    False,
                ]
                if len(new_samples_view) == ctx.dataset.count():
                    self._change_issue_status(
                        ctx, issue_type=issue_type, new_status=STATUS[0]
                    )
            else:
                ctx.panel.state.new_samples[issue_type] = [0, True, False]

        # If all issue types have been checked, mark the scan as complete
        all_checked = all(
            status[1] for status in ctx.panel.state.new_samples.values()
        )
        if all_checked:
            ctx.panel.state.new_sample_scan = False

    def _rescan_samples(self, ctx):
        self.change_computing_status(ctx, ctx.panel.state.issue_type)
        self.navigate_to_screen(
            ctx,
            issue_type=ctx.panel.state.issue_type,
            next_screen="pre_load_compute",
        )

    def change_computing_status(
        self,
        ctx,
        issue_type,
        is_computing=False,
        execution_type="",
        delegation_run_id="",
        delegation_status="",
        issue_status=None,
    ):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        computing_copy = ctx.panel.state.computing
        computing_copy[issue_type] = {
            "is_computing": is_computing,
            "execution_type": execution_type,
            "delegation_run_id": delegation_run_id,
            "delegation_status": delegation_status,
        }

        ctx.panel.state.set("computing", computing_copy)
        content["computing"] = computing_copy

        if issue_status in list(STATUS.values()):
            content["status"][issue_type] = issue_status

        store.set(key, content)

    ###
    # COMPUTATION
    ###

    def computation_handler(self, ctx, check_existing_field=False):
        check_existing_field = ctx.params.get(
            "check_existing_field", check_existing_field
        )

        issue_type = ctx.params.get("original_params", {}).get(
            "issue_type", None
        )

        # If all computation is already done, skip to analysis
        if check_existing_field and not ctx.dataset.exists(
            FIELD_NAME[issue_type], bool=False
        ):
            self.change_computing_status(
                ctx, issue_type, is_computing=True, execution_type="execute"
            )
            self._process_issue_computation(ctx, issue_type)
            return

        run_id = self._get_run_id(ctx)
        if run_id is not None:
            # Delegated execution was chosen
            store = self.get_store(ctx)
            key = self._get_store_key(ctx)
            content = store.get(key)

            # Set issue status to computing
            content["status"][issue_type] = STATUS[1]
            store.set(key, content)

            self.change_computing_status(
                ctx,
                issue_type,
                is_computing=True,
                execution_type="delegate_execution",
                delegation_run_id=str(run_id),
            )
        else:
            # Immediate execution was chosen
            self._process_issue_computation(ctx, issue_type)

    def compute_near_duplicates(self, ctx):
        ctx.prompt(
            NEAR_DUPLICATES_OPERATOR,
            params={
                "model": "clip-vit-base32-torch",
                "batch_size": 8,
                "metric": "cosine",
            },
            skip_prompt=False,
            on_success=self._on_success_near_duplicates,
            on_error=self.cancel_compute,
        )

    def _on_success_near_duplicates(self, ctx):
        run_id = self._get_run_id(ctx)
        if run_id is not None:
            # Delegated execution was chosen
            self._on_compute_option_selected(ctx, "delegate", run_id=run_id)
        else:
            # Immediate execution was chosen
            self._on_compute_option_selected(ctx, "execute")
            self._process_issue_computation(ctx, "near_duplicates")

    def _get_run_id(self, ctx):
        result = ctx.params.get("result", None)
        return result.get("id", None) if result else None

    def _process_exact_duplicates(self, ctx):
        if ctx.dataset.has_field("filehash"):
            view = ctx.dataset.exists("filehash")
            sample_ids, filehashes = view.values(["id", "filehash"])
        else:
            sample_ids, filehashes = [], []

        filehash_counts = Counter(filehashes)
        dup_filehashes = {k for k, v in filehash_counts.items() if v > 1}

        filehash_to_ids = defaultdict(list)
        if dup_filehashes:
            for id, filehash in zip(sample_ids, filehashes):
                if filehash in dup_filehashes:
                    filehash_to_ids[filehash].append(id)

        dup_filehashes = list(dup_filehashes)
        dup_sample_ids = list(filehash_to_ids.items())
        num_duplicates = sum(len(ids) for ids in filehash_to_ids.values())

        return dup_filehashes, dup_sample_ids, num_duplicates

    def _process_issue_computation(self, ctx, issue_type, recompute=False):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        if issue_type == "exact_duplicates":
            (
                dup_filehashes,
                dup_sample_ids,
                num_duplicates,
            ) = self._process_exact_duplicates(ctx)

            content["results"][issue_type]["dup_filehash"] = dup_filehashes
            content["results"][issue_type]["dup_sample_ids"] = dup_sample_ids
            content["counts"][issue_type] = num_duplicates
        else:
            field_name = FIELD_NAME[issue_type]

            # Generate histogram
            counts, edges, _ = ctx.dataset.histogram_values(
                field_name, bins=50
            )
            content["results"][issue_type] = {
                "counts": counts,
                "edges": edges,
            }

            # Save threshold issue counts
            issue_config = ctx.panel.state.issue_config[issue_type]
            lower_threshold, upper_threshold = self.get_plot_defaults(
                ctx, issue_type, issue_config["detect_method"]
            )
            view = ctx.dataset.match(
                (F(field_name) >= lower_threshold)
                & (F(field_name) <= upper_threshold)
            )
            num_issue_samples = len(view)

            content["counts"][issue_type] = num_issue_samples

        # Update last scan details
        if "last_scan" not in content or content["last_scan"] is None:
            content["last_scan"] = {}
        if content["last_scan"].get(issue_type) is None:
            content["last_scan"][issue_type] = {}
        content["last_scan"][issue_type]["timestamp"] = datetime.utcnow()
        content["last_scan"][issue_type]["dataset_size"] = ctx.dataset.count()

        store.set(key, content)

        # Update new samples status
        if (
            ctx.panel.state.new_samples[issue_type][0] > 0
            and ctx.panel.state.new_samples[issue_type][2]
        ):
            new_samples = ctx.panel.state.new_samples
            new_samples[issue_type] = [0, True, True]
            ctx.panel.state.set("new_samples", new_samples)

        # Update computing status
        computing_field = content.get("computing", {}).get(issue_type, {})
        self.change_computing_status(
            ctx,
            issue_type,
            is_computing=False,
            execution_type=computing_field.get("execution_type", ""),
            delegation_run_id=computing_field.get("delegation_run_id", ""),
            delegation_status="",
            issue_status=STATUS[2],
        )

        if not recompute:
            self.navigate_to_screen(
                ctx, issue_type=issue_type, next_screen="analysis"
            )

    def cancel_compute(self, ctx):
        issue_type = ctx.params.get("original_params", {}).get(
            "issue_type", None
        )

        if (
            issue_type
            and ctx.panel.state.computing[issue_type]["execution_type"]
            == "delegate_execution"
        ):
            # @todo cancel delegated operation here
            pass
        else:
            # @todo cancel immediate operation here
            pass

        if issue_type:
            self.change_computing_status(
                ctx, issue_type, is_computing=False, issue_status=STATUS[0]
            )

    def has_store(self, ctx):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        return True if store.get(key) else False

    def check_computing_status(self, ctx):
        issue_type = ctx.params.get("issue_type", None)
        run_id = ctx.params.get("run_id")

        if issue_type is None:
            raise Exception(
                f"check_computing_status issue_type must be valid - got {issue_type}"
            )

        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        exec_type = (
            content.get("computing", {})
            .get(issue_type, {})
            .get("execution_type")
        )

        if exec_type != "delegate_execution":
            last_scan_timestamp = content["last_scan"][issue_type].get(
                "timestamp", None
            )
            if last_scan_timestamp is None:
                last_scan_timestamp = datetime.utcnow()
                content["last_scan"][issue_type][
                    "timestamp"
                ] = last_scan_timestamp
                store.set(key, content)

            # Immediate executions timeout after 10 minutes, so handle that
            if datetime.utcnow() > last_scan_timestamp + timedelta(minutes=10):
                # new status based on saved results
                self.change_computing_status(
                    ctx,
                    issue_type,
                    is_computing=False,
                    issue_status=STATUS[0],
                )

            return

        dos = DelegatedOperationService()

        try:
            delegated_state = dos.get(bson.ObjectId(run_id)).run_state
        except:
            self.change_computing_status(
                ctx, issue_type, issue_status=STATUS[0]
            )
            return

        if delegated_state == "failed":
            self.change_computing_status(
                ctx, issue_type, issue_status=STATUS[0]
            )

            try:
                self._process_issue_computation(ctx, issue_type)
            except:
                ctx.panel.state.alert = f"computation_failed_{issue_type}"
                if ctx.panel.state.issue_type == issue_type:
                    self.navigate_to_screen(
                        ctx, issue_type=issue_type, next_screen="home"
                    )

            return

        if delegated_state == "completed":
            self.change_computing_status(
                ctx,
                issue_type,
                is_computing=False,
                execution_type="delegate_execution",
                delegation_run_id=str(run_id),
                delegation_status=delegated_state.lower(),
                issue_status=STATUS[2],
            )

            self._process_issue_computation(ctx, issue_type, recompute=True)

            if (
                ctx.panel.state.issue_type is not None
                and ctx.panel.state.issue_type == issue_type
                and ctx.panel.state.screen == "pre_load_compute"
            ):
                self.navigate_to_screen(
                    ctx, issue_type=issue_type, next_screen="analysis"
                )
        elif delegated_state is not None:
            self.change_computing_status(
                ctx,
                issue_type,
                is_computing=True,
                execution_type="delegate_execution",
                delegation_run_id=str(run_id),
                delegation_status=delegated_state.lower(),
            )

    ###
    # SCREENS
    ###

    def home_screen(self, panel, ctx):
        title_view = types.TextView(
            title="Find data quality issues in your dataset and act on them.",
            padding="0 0 1rem 0",
            variant="body2",
            color="text.secondary",
        )
        panel.view("title_view", title_view)

        for issue_type in ISSUE_TYPES:
            self._render_issue_card(panel, issue_type, ctx, expanded=False)

    def pre_load_compute_screen(self, panel, issue_type, ctx):
        existing_field_criteria = (
            FIELD_NAME[ctx.panel.state.issue_type]
            in ctx.dataset.get_field_schema()
            and ctx.panel.state.new_samples[issue_type][0] == 0
        )

        # Some (but not all) new samples exist
        new_samples_exist = (
            ctx.panel.state.new_samples[issue_type][0] > 0
            and ctx.dataset.count()
            != ctx.panel.state.new_samples[issue_type][0]
        )
        is_computing = (
            ctx.panel.state.computing
            and ctx.panel.state.computing[issue_type]["is_computing"]
        )
        no_access = missing_min_access_required(ctx, "EDIT")

        self._render_header(panel, issue_type, ctx)

        card_main = panel.v_stack(
            "pre_load_compute",
            gap=2,
            px=4,
            py=2,
            componentsProps={
                "grid": {
                    "sx": {
                        "display": "flex",
                        "flexDirection": "column",
                        "alignItems": "center",
                    }
                },
                "container": {"sx": {"width": "100%"}},
            },
            container=types.PaperContainer(
                sx={
                    "display": "flex",
                    "flexDirection": "column",
                    "width": "100%",
                    "padding": "0px",
                    "textAlign": "center",
                    "alignItems": "center",
                    "height": "calc(100vh - 185px - 110px)",  # 110 is for Teams
                    "minHeight": "400px",
                }
            ),
        )

        self._render_issue_card(card_main, issue_type, ctx)

        card_content = card_main.v_stack(
            "card_content",
            align_x="center",
            align_y="center",
            gap=2,
            componentsProps={
                "grid": {
                    "sx": {
                        "display": "flex",
                        "flexDirection": "column",
                        "alignItems": "center",
                        "justifyContent": "center",
                    }
                },
                "container": {
                    "sx": {
                        "height": "calc(100vh - 410px)",
                        "display": "flex",
                        "flexDirection": "column",
                        "alignItems": "center",
                        "justifyContent": "center",
                    }
                },
            },
        )

        if is_computing:
            loader_schema = {
                "variant": "spinner",
                "color": "base",
                "size": "medium",
                "componentsProps": {
                    "container": {"sx": {"boxShadow": "none"}}
                },
            }
            loader = types.LoadingView(**loader_schema)
            card_content.obj("loader", view=loader)
        elif existing_field_criteria:
            alert_icon = types.ImageView(
                width="48px",
                componentsProps={
                    "container": {
                        "sx": {
                            "alignItems": "center",
                        }
                    }
                },
            )

            card_content.view(
                f"warning_alert",
                view=alert_icon,
                default=IMAGES["alert"],
            )
        else:
            image_icon = types.ImageView(
                width="48px",
                alt=f"{' '.join(issue_type.split('_')).title()} Icon",
            )
            card_content.view(
                f"{issue_type}_image",
                view=image_icon,
                default=IMAGES[issue_type],
            )

        text_view = types.TextView(
            title=f"Find, curate, and act on {' '.join(issue_type.split('_'))} within your dataset easily with FiftyOne.",
            variant="body2",
            padding=0,
            bold=True,
            color="text.primary",
        )
        card_content.view("text_view_compute", view=text_view)

        if existing_field_criteria and not is_computing:
            card_content.md(
                f"It looks like the `{FIELD_NAME[ctx.panel.state.issue_type]}` field already exists on some samples in your dataset. We'll skip over these samples and only scan new samples without this field for {' '.join(issue_type.split('_'))} issues. Would you like to scan them now?",
                "text_view_existing_field",
            )

        if is_computing:
            button_string = f"Scanning Dataset for {' '.join(issue_type.split('_')).title()}"
        else:
            if new_samples_exist:
                button_string = f"Scan {ctx.panel.state.new_samples[issue_type][0]} New Samples for {' '.join(issue_type.split('_')).title()}"
            elif existing_field_criteria:
                button_string = f"Scan For {' '.join(issue_type.split('_')).title()} & Skip Existing Samples with Field"
            else:
                button_string = f"Scan Dataset for {' '.join(issue_type.split('_')).title()}"

        if (
            ctx.panel.state.computing
            and ctx.panel.state.computing[issue_type]["delegation_status"]
            == "completed"
        ):
            card_content.btn(
                f"navigate_to_analysis",
                label="Complete Delegation & Navigate to Analysis",
                variant="contained",
                on_click=self.navigate_to_screen,
                params={
                    "issue_type": issue_type,
                    "next_screen": "analysis",
                },
                disabled=no_access,
                title=(NOT_PERMITTED_TEXT if no_access else ""),
            )
        else:
            if issue_type == "near_duplicates":
                card_content.btn(
                    f"compute_now_button",
                    label=button_string,
                    variant="contained",
                    on_click=self.compute_near_duplicates,
                    disabled=no_access or is_computing,
                    title=(NOT_PERMITTED_TEXT if no_access else ""),
                )
            else:
                operator_button = types.OperatorExecutionButtonView(
                    icon="expand_more",
                    operator=operator_mapping[issue_type],
                    on_success=self.computation_handler,
                    on_error=self.cancel_compute,
                    params={
                        "issue_type": issue_type,
                    },
                    on_option_selected=self._on_compute_option_selected,
                    label=button_string,
                    disabled=no_access or is_computing,
                    title=(NOT_PERMITTED_TEXT if no_access else ""),
                    componentsProps={
                        "container": {
                            "sx": {
                                "display": "flex",
                                "justifyContent": "center",
                            }
                        }
                    },
                )
                card_content.obj(
                    "execution_button",
                    view=operator_button,
                )

        """
        if (
            ctx.panel.state.computing
            and ctx.panel.state.computing[issue_type]["delegation_status"]
            != "completed"
        ):
            # wait time text
            wait_time = self.estimate_execution_wait_time(
                ctx, ctx.panel.state.new_samples[issue_type][0]
            )
            if wait_time < 60:
                wait_time_str = "< 1 minute"
            else:
                wait_time_str = f"~{math.ceil(wait_time)/60} minutes"

            text_view_wait = types.TextView(
                title=f"Estimated wait time: {wait_time_str}",
                variant="body2",
                italic=True,
                padding="6px 0 0 0",
                color="text.primary",
            )
            card_content.view(
                "text_view_estimated_weight", view=text_view_wait
            )
        """

        # scan size info
        scan_count = (
            ctx.panel.state.new_samples[issue_type][0]
            if ctx.panel.state.new_samples[issue_type][0] > 0
            else ctx.dataset.count()
        )

        # computing notification card
        if is_computing:
            main_computing_card = card_content.h_stack(
                "main_computing_card",
                container=types.PaperContainer(
                    sx={
                        "width": "80%",
                        "margin": "35px auto",
                        "padding": "12px",
                        "border": "1px solid grey",
                        "borderRadius": "10px",
                        "display": "flex",
                        "flexDirection": "row",
                        "& > :first-of-type": {
                            "width": "100%",
                            "& > :last-child": {
                                "& > :first-of-type": {
                                    "flex": "stretch",
                                },
                                "& > :last-child": {
                                    "flex": "1",
                                    "minWidth": "100px",
                                },
                            },
                        },
                    }
                ),
                componentsProps={
                    "grid": {
                        "sx": {
                            "display": "flex",
                            "flexDirection": "row",
                            "justifyContent": "space-between",
                            "alignItems": "center",
                        }
                    },
                },
            )

            left_container = main_computing_card.h_stack(
                "compute_card_container"
            )

            left_computing_card = left_container.v_stack("left_computing_card")
            branded_vertical_divider = types.ImageView(
                alt="Divider",
                height="60px",
                componentsProps={
                    "container": {"sx": {"padding": "0px 7px 0px 7px"}}
                },
            )
            left_computing_card.view(
                f"branded_vertical_divider",
                view=branded_vertical_divider,
                default=IMAGES["vertical_line"],
            )
            right_computing_card = left_container.v_stack(
                "right_computing_card"
            )
            right_sub_computing_card_top = right_computing_card.h_stack(
                "right_sub_computing_card_top"
            )
            right_sub_computing_card_bottom = right_computing_card.h_stack(
                "right_sub_computing_card_bottom", gap="3px"
            )
            if (
                ctx.panel.state.computing
                and ctx.panel.state.computing[issue_type]["execution_type"]
                == "delegate_execution"
            ):
                computing_card_title_text = "Scan Scheduled"
                computing_card_helper_text = (
                    "Scheduled data quality scan on this dataset."
                )
            else:
                computing_card_title_text = "Execution in progress"
                computing_card_helper_text = "This may take a few minutes. Keep this panel open to monitor progress."

            computing_card_text_top = types.TextView(
                title=computing_card_title_text,
                padding="0 0 0 0",
                variant="h2",
                bold=True,
                color="text.primary",
            )
            right_sub_computing_card_top.view(
                "computing_card_text_top",
                computing_card_text_top,
            )
            num_samples_text_top = types.TextView(
                title=f"{scan_count:,} samples",
                padding="0 0.5rem 0rem 0.5rem",
                variant="body2",
                color="text.secondary",
                componentsProps={"text": {"sx": {"textAlign": "left"}}},
            )
            right_sub_computing_card_top.view(
                "num_samples_text_top", num_samples_text_top
            )
            computing_card_text_bottom = types.TextView(
                title=computing_card_helper_text,
                padding="0 0 0 0",
                variant="body1",
                color="text.secondary",
                componentsProps={"text": {"sx": {"textAlign": "left"}}},
            )
            right_sub_computing_card_bottom.view(
                "computing_card_text_bottom", computing_card_text_bottom
            )
            if (
                ctx.panel.state.computing
                and ctx.panel.state.computing[issue_type]["execution_type"]
                == "delegate_execution"
            ):
                computing_card_runs_button = types.ButtonView(
                    label="View Status",
                    href=f"/datasets/{ctx.dataset.slug}/runs/{ctx.panel.state.computing[issue_type]['delegation_run_id']}",
                    variant="outlined",
                )

                main_computing_card.view(
                    "computing_card_runs_button",
                    computing_card_runs_button,
                )

    def add_stack(
        self,
        id,
        container,
        container_justify_content="center",
        height="calc(100vh - 185px - 110px)",  # 110px for Teams
        px=0,
        py=0,
        h_stack=False,
    ):
        stack_type = (
            container.v_stack if h_stack is False else container.h_stack
        )
        return stack_type(
            id,
            px=px,
            py=py,
            componentsProps={
                "grid": {
                    "sx": {
                        "display": "flex",
                        "flexDirection": (
                            "column" if h_stack is False else "row"
                        ),
                        "alignItems": "center",
                    }
                },
                "container": {"sx": {"width": "100%"}},
            },
            container=types.PaperContainer(
                sx={
                    "display": "flex",
                    "flexDirection": "column" if h_stack is False else "row",
                    "width": "100%",
                    "textAlign": "center",
                    "alignItems": "center",
                    "height": height,
                    "justifyContent": container_justify_content,
                    "overflowY": "auto",
                    "boxShadow": "none",
                }
            ),
        )

    def analysis_screen(self, panel, issue_type, ctx):
        self._render_header(panel, issue_type, ctx)

        card_main = self.add_stack(f"{issue_type}_analysis", panel)

        card_content = self.add_stack(
            f"{issue_type}_analysis_content",
            card_main,
            container_justify_content="normal",
            height="calc(100vh - 343px - 110px)",  # 110 is for Teams Header
            px=4,
            py=1,
        )
        self._render_issue_card(card_content, issue_type, ctx)

        # sync the grid view
        ctx.ops.set_active_fields(fields=[FIELD_NAME[issue_type]])

        if issue_type == "exact_duplicates":
            # sync the grid view
            ctx.ops.set_active_fields(fields=["filehash", "id"])
            store = self.get_store(ctx).get(self._get_store_key(ctx))
            result = store["results"][issue_type]["dup_sample_ids"]
            if len(result) == 0:
                self._render_no_results(card_content, issue_type)
            elif len(result) > 0:
                self._get_exact_duplicates_tree(card_content, ctx)

        else:
            # sync the grid view
            ctx.ops.set_active_fields(fields=[FIELD_NAME[issue_type]])
            self._get_histogram_screen(card_content, ctx, field=issue_type)

        functionality_stack = self.add_stack(
            f"functionality_stack_{issue_type}",
            card_main,
            height="auto",
            h_stack=True,
            px=2,
        )

        # tagging button side
        tagging_functionality_stack = functionality_stack.v_stack(
            f"tagging_functionality_stack",
            align_y="center",
            align_x="center",
            width="100%",
            componentsProps={
                "grid": {"sx": {"display": "flex", "flexDirection": "column"}},
            },
        )

        tagging_text = self._get_tag_helper_text(ctx)

        tagging_functionality_stack.view(
            f"tagging_cta",
            types.TextView(
                title=tagging_text,
                variant="body2",
                bold=True,
                color="text.secondary",
                padding="0 0 1rem 0",
                font_size="1.1rem",
            ),
        )

        # tagging functionality
        self._get_tagging_modal_screen(
            tagging_functionality_stack,
            ctx,
        )

        # rescan button side
        number_of_new_samples = ctx.panel.state.new_samples[
            ctx.panel.state.issue_type
        ][0]

        rescan_functionality_stack = functionality_stack.v_stack(
            f"rescan_functionality_stack",
            align_y="center",
            align_x="center",
            componentsProps={
                "grid": {"sx": {"display": "flex", "flexDirection": "column"}},
            },
        )
        rescan_text_h_stack = rescan_functionality_stack.h_stack(
            f"rescan_text_h_stack",
            componentsProps={
                "grid": {
                    "sx": {
                        "display": "inline-flex",
                        "width": "auto",
                        "alignItems": "center",
                    }
                }
            },
        )

        if (
            number_of_new_samples > 0
            and not ctx.panel.state.new_samples[issue_type][2]
        ):
            alert_icon = types.ImageView(
                width="12px",
                componentsProps={
                    "container": {
                        "sx": {
                            "position": "relative",
                            "top": "-4px",
                            "alignItems": "center",
                        }
                    }
                },
            )

            rescan_text_h_stack.view(
                f"orange_alert_icon",
                view=alert_icon,
                default=IMAGES["alert_in_circle"],
            )

        rescan_text_h_stack.view(
            f"rescan_cta",
            types.TextView(
                title=f"{number_of_new_samples} new sample{'s' if number_of_new_samples != 1 else ''} detected:",
                variant="body2",
                bold=True,
                color=f"{'#FFC399' if number_of_new_samples > 0 else 'text.secondary'}",
                padding="0 0 1rem 0",
                font_size="1.1rem",
            ),
        )

        # rescan functionality
        self._get_rescan_modal_screen(rescan_functionality_stack, ctx)

    def wrapper_screen(self, panel, which_wrapper):
        card_main = self.add_stack("pre_load_compute", panel)
        wrapper_dataset_image = types.ImageView(
            width="48px",
            alt=f"{which_wrapper.title()} Image",
        )
        card_main.view(
            f"{which_wrapper}dataset_image",
            view=wrapper_dataset_image,
            default=(
                IMAGES["data_quality"]
                if which_wrapper == "entry"
                else IMAGES["unsupported_dataset"]
            ),
        )
        wrapper_info = types.TextView(
            title=f"Data Quality helps you find and act on quality issues in your dataset.",
            variant="body2",
            padding=1,
            bold=False,
            color="text.primary",
        )
        card_main.view("wrapper_info", view=wrapper_info)
        if which_wrapper == "unsupported":
            unsupported_note = types.TextView(
                title=f"This panel currently only supports image datasets.",
                variant="body2",
                padding=0,
                bold=True,
                color="text.primary",
            )
            card_main.view("unsupported_note", view=unsupported_note)
        elif which_wrapper == "entry":
            card_main.btn(
                f"get_started",
                label=f"Get Started",
                variant="contained",
                on_click=self.navigate_to_screen,
                icon="arrow_forward",
                icon_position="right",
            )

    def _render_issue_status_badge(self, panel, issue_type, ctx):
        status_dict = {
            STATUS[0]: STATUS_COLOR[0],
            STATUS[1]: STATUS_COLOR[1],
            STATUS[2]: STATUS_COLOR[2],
            STATUS[3]: STATUS_COLOR[3],
        }
        current_status = self._get_issue_status(ctx, issue_type)
        current_screen = ctx.panel.state.screen

        no_access = missing_min_access_required(ctx, "EDIT")

        badge_schema = {
            "text": current_status,
            "color": status_dict[current_status],
            "read_only": no_access,
            "componentsProps": {
                "pillBadge": {
                    "tooltipTitle": (NOT_PERMITTED_TEXT if no_access else ""),
                }
            },
        }

        list = [[STATUS[2], STATUS_COLOR[2]], [STATUS[3], STATUS_COLOR[3]]]

        if current_status == STATUS[0]:
            pass
        elif current_status == STATUS[3] and current_screen == "analysis":
            list = [list[1], list[0]]
            badge_schema["text"] = list
        elif current_status == STATUS[2] and current_screen == "analysis":
            badge_schema["text"] = list

        badge = types.PillBadgeView(**badge_schema)
        panel.view(
            f"status_badge_{issue_type}",
            view=badge,
            on_change=self._change_issue_status,
        )

    def _render_issue_threshold_config(self, panel, issue_type, ctx):
        no_access = missing_min_access_required(ctx, "EDIT")
        dropdown = types.DropdownView(
            icon="SettingsIcon",
            addOnClickToMenuItems=True,
            readOnly=no_access,
            tooltipTitle=(NOT_PERMITTED_TEXT if no_access else ""),
            componentsProps={
                "select": {
                    "sx": {
                        "& .MuiSelect-select": {
                            "paddingRight": "0px !important",
                        }
                    }
                },
                "optionContainer": {
                    "addOnClickToMenuItems": True,
                },
            },
        )
        dropdown.add_choice("Save Threshold", label="Save Threshold")
        dropdown.add_choice("Reset Threshold", label="Reset Threshold")
        panel.view(
            f"{issue_type}_threshold_setting_menu",
            view=dropdown,
            on_change=self.on_change_set_threshold,
        )

    def _render_issue_navigation(self, panel, issue_type, ctx):
        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        store_content = store.get(key)

        last_scan_timestamp = (
            ctx.panel.state.last_scan.get(issue_type) or {}
        ).get("timestamp", None)

        last_scan = (
            store_content.get("last_scan", {}).get(issue_type, None) or {}
        )

        last_dataset_size = (
            ctx.panel.state.last_scan.get(issue_type) or {}
        ).get(
            "dataset_size",
            last_scan.get("dataset_size", None) or ctx.dataset.count(),
        ) or 0

        recompute = False

        if (
            last_scan_timestamp is not None or last_scan.get("timestamp", None)
        ) and not (
            ctx.dataset.count() == ctx.panel.state.new_samples[issue_type][0]
        ):
            if ctx.panel.state.new_samples[issue_type][1]:
                if (ctx.panel.state.new_samples[issue_type][0] == 0) or (
                    last_dataset_size >= ctx.dataset.count()
                ):
                    recompute = True
                    next_screen = "analysis"
                else:
                    next_screen = "analysis"
            else:
                next_screen = "analysis"
        else:
            next_screen = "pre_load_compute"

        panel.btn(
            f"collapsed_icon_{issue_type}",
            label=None,
            icon="arrow_forward",
            variant="filled",
            on_click=self.navigate_to_screen,
            params={
                "issue_type": issue_type,
                "next_screen": next_screen,
                "recompute": recompute,
            },
        )

    def _render_issue_card(self, panel, issue_type, ctx, expanded=True):
        header_card = panel.h_stack(
            f"header_{issue_type}",
            align_x="space-between",
            align_y="center",
            container=types.PaperContainer(
                sx={
                    "boxShadow": "none",
                    "padding": "0 0 0 1rem" if expanded else "1rem",
                }
            ),
        )

        sub_card_left = header_card.h_stack(
            f"collapsed_sub_left_{issue_type}",
            align_x="start",
            align_y="baseline",
            orientation="horizontal",
            container=types.PaperContainer(
                sx={
                    "width": "100%",
                    "boxShadow": "none",
                }
            ),
        )

        sub_card_left.view(
            f"collapsed_sub_left_{issue_type}",
            types.TextView(
                title=" ".join(issue_type.split("_")).title(),
                variant="body2",
                font_size="1.35rem",
                bold=True,
                color="text.primary",
                padding=0,
            ),
        )

        screen = ctx.panel.state.screen

        issue_count = self._get_issue_count(ctx, issue_type)
        if (screen == "home") and (
            issue_count is not None
            and issue_count > 0
            and self._get_issue_status(ctx, issue_type) == STATUS[2]
        ):
            sub_card_left.view(
                f"issue_count_{issue_type}_home_page",
                types.TextView(
                    title=f"{issue_count} Potential Issue{'s' if (issue_count > 1 or issue_count == 0) else ''}",
                    variant="body2",
                    padding="0 1 0 1",
                    color="text.secondary",
                ),
            )

        if (screen == "analysis") and (
            self._get_current_issue_count(ctx, issue_type) is not None
            and self._get_issue_status(ctx, issue_type) == STATUS[2]
        ):
            current_count = self._get_current_issue_count(ctx, issue_type)
            sub_card_left.view(
                f"issue_count_{issue_type}_analysis_page",
                types.TextView(
                    title=f"{current_count} Potential Issue{'s' if (current_count > 1 or current_count == 0) else ''}",
                    variant="body2",
                    padding="0 1 0 1",
                    color="text.secondary",
                ),
            )

        if (
            screen == "home"
            and ctx.panel.state.new_samples[issue_type][0] > 0
            and not ctx.panel.state.new_samples[issue_type][2]
            and ctx.panel.state.new_samples[issue_type][0]
            != ctx.dataset.count()
        ):
            sub_card_left.view(
                f"collapsed_issue_new_sample_count_{issue_type}",
                types.TextView(
                    title=f"{ctx.panel.state.new_samples[issue_type][0]} New Sample{'s' if (ctx.panel.state.new_samples[issue_type][0] > 1 or ctx.panel.state.new_samples[issue_type][0] == 0) else ''} Detected",
                    variant="body2",
                    padding="0 1 0 1",
                    color="#FFC399",
                ),
            )

        # issue status badge
        sub_card_right = header_card.h_stack(
            f"expanded_sub_right_{issue_type}",
            align_x="end",
            align_y="baseline",
            orientation="horizontal",
            container=types.PaperContainer(
                sx={
                    "width": "100%",
                    "boxShadow": "none",
                }
            ),
        )
        self._render_issue_status_badge(sub_card_right, issue_type, ctx)

        # issue threshold config menu
        if (
            expanded
            and screen == "analysis"
            and issue_type != "exact_duplicates"
        ):
            self._render_issue_threshold_config(
                sub_card_right, issue_type, ctx
            )
        elif screen == "home":
            self._render_issue_navigation(sub_card_right, issue_type, ctx)

    def _render_header(self, panel, issue_type, ctx):
        review_warning = (
            ctx.panel.state.screen == "analysis"
            and self._get_issue_status(ctx, issue_type) == STATUS[2]
            and not missing_min_access_required(ctx, "EDIT")
        )
        card_header = panel.h_stack(
            "navbar",
            align_x="left",
            align_y="center",
            gap="3px" if review_warning else "8px",
            componentsProps={"container": {"sx": {"padding": "0 0 .5rem 0"}}},
        )

        if review_warning:
            schema = {
                "modal": {
                    "title": f"Review Pending {' '.join(issue_type.split('_')).title()} Issues",
                    "body": f"Before you leave, let your team know you've identified {' '.join(issue_type.split('_'))} issues in your dataset.\nAre you sure you want to leave without marking these issues as reviewed?",
                    "icon": "warning_amber",
                    "iconVariant": "filled",
                },
                "primaryButton": {
                    "primaryText": "Mark as Reviewed",
                    "primaryColor": "#FFFFFF",
                    "backgroundColor": "#509553",
                    "&:hover": {"backgroundColor": "#509553"},
                },
                "secondaryButton": {
                    "secondaryText": "I'm not ready yet",
                    "secondaryColor": "secondary",
                    "params": {"next_screen": "home"},
                },
                "primaryCallback": self.mark_as_reviewed_in_modal,
                "secondaryCallback": self.navigate_to_screen,
            }

            review_warning_modal = types.ModalView(
                **schema,
                label=None,
                variant="filled",
                icon="arrow_back",
                width="38px",
                padding="6px 0px 6px 9px",
            )

            card_header.obj(
                "back_button_with_warning", view=review_warning_modal
            )
        else:
            card_header.btn(
                f"back_button",
                label=None,
                icon="arrow_back",
                variant="filled",
                on_click=self.navigate_to_screen,
                params={"next_screen": "home"},
            )

        card_header.view(
            "title_view",
            types.TextView(
                title="All data quality issue types",
                variant="body2",
                color="text.secondary",
                padding="0",
            ),
        )

    def _render_toast(self, panel, toast_type, ctx):
        view = {
            "duration": 3000,
            "layout": {
                "horizontal": "center",
                "top": "50px",
                "fontSize": "15px",
                "textAlign": "center",
            },
        }

        if ctx.panel.state.issue_type is not None:
            issue_type = " ".join(ctx.panel.state.issue_type.split("_"))

            if toast_type == "tagging":
                message = f"Selected samples tagged with: {', '.join(ctx.panel.state.tags)}"
                view["message"] = message
            elif toast_type == "reviewed":
                message = (
                    f"{issue_type.capitalize()} issues marked as reviewed!"
                )
                view["message"] = message
            elif "computation_failed" in toast_type:
                issue_type = " ".join(toast_type.split("_")[2:])
                message = (
                    f"Scan for {issue_type} issues failed. Please try again."
                )
                view["message"] = message
                view["layout"]["backgroundColor"] = "#d32f2f"
            else:
                return

        if toast_type == "reviewed" and not ctx.panel.state.issue_type:
            message = "Marked as reviewed."
            view["message"] = message

        toast = types.ToastView(**view)

        panel.obj(f"toast_{uuid.uuid4().hex}", view=toast)

        # Need to clear the alert status after it's been shown
        ctx.panel.state.alert = ""

    def _render_no_results(self, stack, issue_type):
        no_result_container = stack.v_stack(
            "no_result_container",
            align_x="center",
            align_y="center",
            gap=2,
            componentsProps={
                "grid": {
                    "sx": {
                        "display": "flex",
                        "flexDirection": "column",
                        "minHeight": "50vh",
                    }
                }
            },
        )

        image_icon = types.ImageView(
            width="48px",
            alt=f"{' '.join(issue_type.split('_')).title()} Icon",
        )
        no_result_container.view(
            f"{issue_type}_image",
            view=image_icon,
            default=IMAGES[issue_type],
        )

        text_view = types.TextView(
            title=f"No {' '.join(issue_type.split('_'))} issues found within your dataset.",
            variant="caption",
            padding=0,
            bold=True,
            color="text.primary",
            align="center",
        )

        no_result_container.view(
            f"no_result_view_{issue_type}",
            view=text_view,
            componentsProps={"text": {"sx": {"textAlign": "center"}}},
        )

    def _get_exact_duplicates_tree(self, stack, ctx):
        store_content = self.get_store(ctx).get(self._get_store_key(ctx))
        result = store_content["results"]["exact_duplicates"]["dup_sample_ids"]
        tree_view = types.TreeSelectionView(data=result)
        stack.view(
            "exact_duplicate_selections",
            view=tree_view,
            on_change=self.toggle_select,
        )

    def _get_histogram_screen(self, stack, ctx, field):
        self.set_hist_defaults(ctx)

        upper_thresh = ctx.panel.state.hist_upper_thresh
        lower_thresh = ctx.panel.state.hist_lower_thresh

        store = self.get_store(ctx)
        key = self._get_store_key(ctx)
        content = store.get(key)

        # edge case: has the field but no results in store
        if content["results"][field]["edges"] is None:
            counts, edges, _ = ctx.dataset.histogram_values(
                FIELD_NAME[field], bins=50
            )
            content["results"][field] = {"counts": counts, "edges": edges}
            store.set(key, content)
        else:
            counts = content["results"][field]["counts"]
            edges = content["results"][field]["edges"]

        min_v, max_v = ctx.dataset.bounds(FIELD_NAME[field])

        (
            in_threshold_counts,
            out_of_threshold_counts,
        ) = self.prepare_histogram_data(
            counts, edges, lower_thresh, upper_thresh
        )

        trace_in_threshold = {
            "x": edges[:-1],  # Use the start of each range for x-values
            "y": in_threshold_counts,
            "type": "bar",
            "name": f"Samples in your threshold with {TITLE[field]} issues",
            # "hoverTemplate": '<p>Range</p>: $%{x:.3f}' +
            #             '<br>Count: %{y}<br>' +
            #             '<b>%{Sample with issues}</b>'
            # "marker": {"color": "#ECD000"},
        }

        trace_out_of_threshold = {
            "x": edges[:-1],  # Use the start of each range for x-values
            "y": out_of_threshold_counts,
            "type": "bar",
            "name": "Other samples",
            # "marker": {"color": "#D3D3D3"},
        }

        # Pass these traces to Plotly to overlay
        histogram_data = [trace_out_of_threshold, trace_in_threshold]
        custom_field_titles = {"near_duplicates": "Nearest Neighbor Distance"}

        layout = {
            "autosize": True,  # Enable autosizing for responsive behavior
            "responsive": True,
            "height": 400,
            "xaxis": {
                "title": custom_field_titles.get(
                    field, " ".join(field.split("_")).title()
                ),
                "tickmode": "auto",
                "nticks": 10,
                "ticks": "inside",
                "hoverformat": ".2f",
                # "tickformat": ".0%",  # Format x-axis as percentages
                "showgrid": False,  # Remove grid lines
            },
            "yaxis": {
                "title": "Count",
                "showgrid": False,  # Remove grid lines
            },
            "bargap": 0.05,
            "dragmode": "select",
            "selectdirection": "h",
            "showlegend": True,
            "legend": {
                "x": 0.5,
                "y": -0.4,
                "xanchor": "center",
                "orientation": "h",
                "bgcolor": "rgba(0, 0, 0, 0)",  # Transparent background for the legend
            },
            "barmode": "overlay",  # Overlay the two histograms
            "plot_bgcolor": "rgba(0, 0, 0, 0)",  # Transparent background for the plotting area
            "paper_bgcolor": "rgba(0, 0, 0, 0)",  # Transparent background for the entire layout
            "colorway": ["grey", "#FF6D04", "blue"],
        }

        # ctx.panel.set_data("histogram", histogram_data)
        # ctx.panel.set_data("layout", layout)

        wrapper_stack = stack.h_stack(
            "plot_wrapper",
            px=0,
            py=0,
            componentsProps={
                "grid": {
                    "sx": {
                        "display": "flex",
                    }
                },
                "container": {"sx": {"minHeight": "400px"}},
            },
        )

        # Bar Chart - Histogram
        wrapper_stack.plot(
            f"{field}-histogram",
            data=histogram_data,
            layout=layout,
            config={
                "scrollZoom": False,
                "displayModeBar": False,
                "responsive": True,
            },
        )

        # Double Slider
        stack.view(
            f"double_slider_{field}",
            on_change=self.slider_change,
            view=types.SliderView(
                value_precision=3,
                variant="withInputs",
                min=min_v,  # Fixed range min
                max=max_v,  # Fixed range max
                label="Threshold",
                value_label_display="auto",
            ),
        )

    def _get_tagging_modal_screen(self, stack, ctx):
        selected_samples_string = self._get_tag_helper_text(ctx)

        no_access = missing_min_access_required(ctx, "TAG")

        schema = {
            "modal": {
                "title": selected_samples_string,
                "subtitle": "Apply Tags",
                "body": 'Once tags are applied, you can filter your dataset using these tags via the sidebar. These tags will be visible under the "Sample Tags" section.',
                "icon": "local_offer",
                "iconVariant": "outlined",
            },
            "primaryButton": {
                "primaryText": "Apply Tags to Samples",
                "primaryColor": "primary",
                "params": {"selected_samples": ctx.selected},
            },
            "secondaryButton": {
                "secondaryText": "Cancel",
                "secondaryColor": "secondary",
            },
            "primaryCallback": self._tag_samples,
            "functionality": "tagging",
        }

        tagging_modal = types.ModalView(
            **schema,
            label="Add Tags",
            variant="outlined",
            icon="local_offer",
            disabled=(
                no_access
                or self._get_issue_status(ctx, ctx.panel.state.issue_type)
                == STATUS[3]
                or self._get_current_issue_count(
                    ctx, ctx.panel.state.issue_type
                )
                == 0
            ),
            title=NOT_PERMITTED_TEXT if no_access else "",
        )

        stack.obj("tagging_modal_button", view=tagging_modal)

    def _get_rescan_modal_screen(self, stack, ctx):
        number_of_new_samples = ctx.panel.state.new_samples[
            ctx.panel.state.issue_type
        ][0]

        schema = {
            "modal": {
                "title": f"New Samples Detected",
                "subtitle": "Rescan Available",
                "body": f'It looks like there are {number_of_new_samples} samples on your dataset that have not yet been scanned for {" ".join(ctx.panel.state.issue_type.split("_"))} issues. Would you like to scan them now?',
                "icon": "search",
                "iconVariant": "filled",
            },
            "primaryButton": {
                "primaryText": f"Navigate To New Sample{'s' if number_of_new_samples != 1 else ''} Scan",
                "primaryColor": "primary",
                "disabled": number_of_new_samples == 0,
            },
            "primaryCallback": self._rescan_samples,
        }

        no_access = missing_min_access_required(ctx, "EDIT")

        rescan_modal = types.ModalView(
            **schema,
            label="Scan New Samples",
            variant="outlined",
            icon="search",
            disabled=number_of_new_samples == 0 or no_access,
            title=(
                NOT_PERMITTED_TEXT
                if no_access
                else "No new samples"
                if number_of_new_samples == 0
                else ""
            ),
        )

        stack.obj("rescan_modal_button", view=rescan_modal)

    ###
    # RENDER
    ###

    def render(self, ctx):
        if ctx.dataset is None:
            return types.Property(types.Object())

        panel = types.Object()

        old_dataset_name = ctx.panel.state.dataset_name
        new_dataset_name = ctx.dataset.name

        # NOTE: if dataset changes, the render function is called on the new
        # dataset with the old dataset's store before it gets called again with
        # the right store. This can cause inconsistency in the UX
        if self.has_store(ctx) and new_dataset_name == old_dataset_name:
            if ctx.dataset.media_type != "image":
                self.wrapper_screen(panel, "unsupported")
            elif ctx.panel.state.first_open:
                self.wrapper_screen(panel, "entry")
            else:
                if ctx.panel.state.screen == "home":
                    self.home_screen(panel, ctx)
                elif ctx.panel.state.screen == "pre_load_compute":
                    self.pre_load_compute_screen(
                        panel, ctx.panel.state.issue_type, ctx
                    )
                elif ctx.panel.state.screen == "analysis":
                    self.analysis_screen(
                        panel, ctx.panel.state.issue_type, ctx
                    )
                else:
                    self.home_screen(panel, ctx)

                if ctx.panel.state.alert == "tagging":
                    self._render_toast(panel, "tagging", ctx)
                elif ctx.panel.state.alert == "reviewed":
                    self._render_toast(panel, "reviewed", ctx)
                elif "computation_failed" in (ctx.panel.state.alert or ""):
                    self._render_toast(panel, ctx.panel.state.alert, ctx)

            # check for new samples in the background
            if ctx.panel.state.new_sample_scan:
                panel.view(
                    "new_sample_scan_check",
                    view=types.TimerView(
                        timeout=2000,
                        on_timeout=self.check_for_new_samples,
                    ),
                )

            # delegated op exists, initiate long polling
            if ctx.panel.state.computing:
                for issue_type, scan in ctx.panel.state.computing.items():
                    del_run_id = scan.get("delegation_run_id", "")
                    is_computing = scan.get("is_computing", False)

                    if is_computing:
                        panel.view(
                            issue_type,
                            view=types.TimerView(
                                interval=15000,
                                on_interval=self.check_computing_status,
                                params={
                                    "issue_type": issue_type,
                                    "run_id": del_run_id,
                                },
                            ),
                        )

        return types.Property(panel, view=types.GridView(gap=1.5, px=2, py=2))
