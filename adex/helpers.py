from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import List, Set, Tuple, Optional

from adex.type_aliases import Gene, ConditionName, Color
from adex.models import Condition, METADATA_COLUMNS, DataLoader, ConditionDataLoader, ConditionTissueDataLoader, \
    FileDataLoader, ConditionSequencingTissueDataLoader, DATASET_INFO_COLUMNS, ConditionSequencingDataLoader
from polars import DataFrame
import polars as pl
import pandas as pd
import numpy as np
from pandas.core.series import Series
from matplotlib import pyplot as plt

from sklearn.model_selection import cross_val_score, GridSearchCV
from sklearn.metrics import confusion_matrix, ConfusionMatrixDisplay, accuracy_score, precision_score, recall_score, f1_score, RocCurveDisplay, precision_recall_curve, PrecisionRecallDisplay
from sklearn.model_selection import LearningCurveDisplay, learning_curve


def load_data_per_condition(condition: Condition, path: str) -> List[DataFrame]:
    """
    Loads all the datasets of a certain condition in a list of dataframes
    """

    results = [
        pl.read_parquet(file)
        for file in Path(f"{path}/{condition.name}").glob('*.parquet')
    ]

    if len(results) == 0:
        raise ValueError(f"Possibly wrong path '{path}' provided for files")

    return results


def gene_intersection(dataframes: List[DataFrame]) -> Set[Gene]:
    """
    Returns all the common genes found in a list of dataframes
    """
    common_genes = set()

    for df in dataframes:
        if len(common_genes) == 0:  # First iteration
            common_genes.update(
                df.select("gene").to_series().to_list()
            )
        else:
            common_genes.intersection_update(
                set(df.select("gene").to_series().to_list())
            )

    return common_genes


def common_genes_dataframe(dataframes: List[DataFrame]) -> DataFrame:
    """
    Gives a dataframe with the samples of all the dataframes joined but only for the common genes
    """
    head, *tail = dataframes

    return reduce(
        lambda left, right: left.join(right, on="gene", how="inner"),
        tail,
        head
    )


def high_frequency_genes_dataframe(
    dataframes: List[DataFrame],
    allowed_null_percentage: float = 0.2,
    drop_frequencies_column: bool = True
) -> DataFrame:
    """
    Gives a dataframe with the samples of all the dataframes joined, but only for the genes that appear in a specific
    percent of the samples.

    :param dataframes: dataframes to be joined
    :param allowed_null_percentage: will keep only genes that have a lower than this null percentage across samples
    :param drop_frequencies_column: if frequencies column should be dropped (or kept for exploratory analysis)
    :return:
    """
    head, *tail = dataframes

    outer_joined_df = reduce(
        lambda left, right: left.join(right, on="gene", how="outer_coalesce"),
        tail,
        head
    )

    filtered_df = (
        outer_joined_df.with_columns(pl.sum_horizontal(pl.all().is_null() / pl.all().count()).alias("Null-Percentage"))
        .filter(pl.col("Null-Percentage") <= allowed_null_percentage)
    )

    if drop_frequencies_column:
        return filtered_df.drop("Null-Percentage")

    return filtered_df


def get_pre_processed_dataset(
    data_loader: DataLoader,
    data_path: str,
    metadata_path: str,
    datasets_info_path: str,
    allowed_null_percentage: float = 0.2,
    return_metadata: bool = True,
) -> Optional[DataFrame]:
    """
    :param data_loader: determines the subset of the data that will be loaded
    :param data_path: the path where the samples are located
    :param metadata_path: the path where the metadata of the samples is located
    :param datasets_info_path: the path where the datasets extra information is located
    :param allowed_null_percentage: will keep only genes that have a lower than this null percentage across samples
    :param return_metadata: if the metadata columns will be returned as part of the dataframe
    :return: a dataset of a particular condition/sequencing-method/tissue/file pre-processed in its final state
    """

    match data_loader:
        case FileDataLoader(condition, file_name, _, _):
            data: List[DataFrame] = [pl.read_parquet(f"{data_path}/{condition.name}/{file_name}")]
        case _:
            data: List[DataFrame] = load_data_per_condition(data_loader.condition, data_path)

    # keep only frequent genes between datasets
    # NOTE: Commenting! This is better to happen later after we apply more filtering, otherwise we end-up
    #   with many nulls after the second filtering!
    # data_frequent_genes: DataFrame = high_frequency_genes_dataframe(data, allowed_null_percentage)

    # Join all dataframes (used to happen in `high_frequency_genes_dataframe` before)
    head, *tail = data
    joined_df: DataFrame = reduce(
        lambda left, right: left.join(right, on="gene", how="outer_coalesce"),
        tail,
        head
    )

    # Transpose
    transposed = joined_df.transpose(include_header=True, header_name='Sample')
    transposed_fixed = (
            transposed
            .rename(transposed.head(1).to_dicts().pop())    # add header
            .slice(1,)                                      # remove first row because it is the header duplicated
            .rename({"gene": "Sample"})                     # fix header
    )

    # Change type of numerical columns
    sample_col = transposed_fixed.select("Sample")
    transposed_fixed = transposed_fixed.select(pl.exclude("Sample")).cast(pl.Float64)
    transposed_fixed = sample_col.with_columns(transposed_fixed)

    # join with various metadata files and keep a sample only if metadata exists for the sample
    datasets_info = pl.read_csv(datasets_info_path)

    transposed_fixed_w_metadata = transposed_fixed.join(
        pl.read_csv(metadata_path).unique(subset=["Sample"]),  # Filters duplicate rows for a sample in metadata
        on="Sample",
        how="inner"
    ).join(
        datasets_info,
        left_on="GSE",
        right_on="Dataset",
        how="inner"
    )

    # Extra data filtering
    match data_loader:
        case ConditionTissueDataLoader(_, tissue):
            transposed_fixed_w_metadata = transposed_fixed_w_metadata.filter(pl.col("Tissue") == tissue.value)
        case ConditionSequencingDataLoader(_, sequencing_technique):
            transposed_fixed_w_metadata = (
                transposed_fixed_w_metadata
                .filter(pl.col("Method") == sequencing_technique.value)
            )
        case FileDataLoader(_, _, genes, samples):
            if genes is not None:
                transposed_fixed_w_metadata = _keep_only_selected_genes(transposed_fixed_w_metadata, genes)
            if samples is not None:
                transposed_fixed_w_metadata = _keep_only_selected_samples(transposed_fixed_w_metadata, samples)
        case ConditionSequencingTissueDataLoader(_, sequencing_technique, tissue, genes):
            transposed_fixed_w_metadata = (
                transposed_fixed_w_metadata
                .filter((pl.col("Tissue") == tissue.value) & (pl.col("Method") == sequencing_technique.value))
            )

            if genes is not None:
                transposed_fixed_w_metadata = _keep_only_selected_genes(transposed_fixed_w_metadata, genes)
        case _:
            pass  # nothing to do

    if transposed_fixed_w_metadata.shape[0] == 0:  # No rows
        return None

    # Drop a column if nulls exceed the 'allowed_null_percentage':
    transposed_fixed_w_metadata = transposed_fixed_w_metadata[[s.name for s in transposed_fixed_w_metadata if ((s.null_count() / transposed_fixed_w_metadata.height) <= allowed_null_percentage)]]

    if return_metadata:
        return transposed_fixed_w_metadata
    else:
        return transposed_fixed_w_metadata.drop(METADATA_COLUMNS).drop(DATASET_INFO_COLUMNS)


def _keep_only_selected_genes(input: DataFrame, genes: List[str]) -> DataFrame:
    existing_columns: Set[str] = set(input.columns)

    fixed_columns: Set[str] = {"Sample"}.union(METADATA_COLUMNS).union(DATASET_INFO_COLUMNS).intersection(existing_columns)
    fixed_columns_and_genes: Set[str] = fixed_columns.union(set(genes))
    common_columns_set: Set[str] = fixed_columns_and_genes.intersection(existing_columns)

    common_columns: List[str] = ["Sample"] + list(fixed_columns - {"Sample"}) + list(common_columns_set - fixed_columns)  # Re-ordering
    return input.select(common_columns)


def _keep_only_selected_samples(input: DataFrame, samples: List[str]) -> DataFrame:
    return input.filter(pl.col("Sample").is_in(samples))


@dataclass(frozen=True)
class PlottingColorParameters:
    """
    This class is used to pass the plotting color parameters
    """
    column_that_defines_colors: Series
    target_colors: List[Tuple[ConditionName, Color]]


def plot_condition_2d(
        data_loader: DataLoader,
        method: str,
        x_label: str,
        y_label: str,
        df_to_plot: pd.DataFrame,
        plotting_color_parameters: PlottingColorParameters
) -> None:
    plt.figure()
    plt.figure(figsize=(10, 10))
    plt.xticks(fontsize=12)
    plt.yticks(fontsize=14)

    plt.xlabel(x_label, fontsize=20)
    plt.ylabel(y_label, fontsize=20)
    match data_loader:
        case ConditionDataLoader(condition):
            plt.title(f"{method} of '{condition.name}' Dataset", fontsize=20)
        case ConditionTissueDataLoader(condition, tissue):
            plt.title(f"{method} of '{condition.name}|{tissue.value}' Dataset", fontsize=20)
        case FileDataLoader(condition, file_name, _, _):
            plt.title(f"{method} of '{condition.name}|{file_name}' Dataset", fontsize=20)
        case ConditionSequencingDataLoader(condition, sequencing_technique):
            plt.title(f"{method} of '{condition.name}|{sequencing_technique.name}' Dataset", fontsize=20)
        case ConditionSequencingTissueDataLoader(condition, sequencing_technique, tissue, _):
            plt.title(f"{method} of '{condition.name}|{sequencing_technique.name}|{tissue.value}' Dataset", fontsize=20)
        case _:
            raise ValueError(f"DataLoader '{data_loader}' not handled in plotting")

    for target, color in plotting_color_parameters.target_colors:
        indices = plotting_color_parameters.column_that_defines_colors == target
        plt.scatter(
            df_to_plot.loc[indices, x_label],
            df_to_plot.loc[indices, y_label],
            c=color,
            s=50
        )

    targets = [target for target, _ in plotting_color_parameters.target_colors]
    plt.legend(targets, prop={'size': 15})


def run_ml_model(classifier, x_train, y_train, x_test, y_test, cv=4, param_grid=None):
    raveled_y_train = np.ravel(y_train)
    base_model = classifier.fit(x_train, raveled_y_train)
    print(f"Default Parameters of Base Model: {base_model.get_params()}")

    # Possibly hyperparameter tuning with cross validation
    if param_grid is not None:
        print(f"Parameters for tuning provided: {param_grid}")

        grid_search_cv = GridSearchCV(
            estimator=classifier,
            param_grid=param_grid,
            cv=cv,
            verbose=1
        ).fit(x_train, raveled_y_train)

        selected_model = grid_search_cv.best_estimator_
        print("Running with hyper-parameter tuned model")
        print(f"Optimised Model Parameters: {selected_model.get_params()}")
    else:
        print("Running with base model")
        selected_model = base_model

    # Cross Validation
    scores = cross_val_score(selected_model, x_train, raveled_y_train, cv=cv)
    print(f"Cross Validation Scores (cv={cv}): {','.join([str(score) for score in scores])}")
    print("Cross Validation gives %0.2f accuracy with a standard deviation of %0.2f" % (scores.mean(), scores.std()))

    # Learning Curve
    # train_sizes, train_scores, test_scores = learning_curve(selected_model, x_train, y_train)
    # learning_curve_display = LearningCurveDisplay(
    #     train_sizes=train_sizes,
    #     train_scores=train_scores,
    #     test_scores=test_scores,
    #     score_name="Score"
    # )
    # learning_curve_display.plot()

    # Test set
    prediction = selected_model.predict(x_test)
    print("\nMetrics on the Test Set:")

    ConfusionMatrixDisplay(
        confusion_matrix=confusion_matrix(y_test, prediction),
        display_labels=["Healthy", "Diseased"]
    ).plot()

    # roc_curve = RocCurveDisplay.from_estimator(selected_model, x_test, y_test)

    # precision, recall, _ = precision_recall_curve(y_test, prediction)
    # precision_recall_curve_result = PrecisionRecallDisplay(precision=precision, recall=recall)
    # precision_recall_curve_result.plot()

    print(f"""
        Accuracy: {accuracy_score(y_test, prediction)}
        Precision: {precision_score(y_test, prediction)}
        Recall: {recall_score(y_test, prediction)}
        f1: {f1_score(y_test, prediction)}
    """)

    return selected_model, prediction
