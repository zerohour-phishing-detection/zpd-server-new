from collections.abc import AsyncIterator

from aiostream import pipe, stream
from requests_html import HTMLSession
from sklearn.linear_model import LogisticRegression

import utils.region_detection as region_detection
from logo_finders.base import LogoFinder
from search_engines.image.base import ReverseImageSearchEngine
from settings.dst import DSTSettings
from utils.async_threads import ThreadWorker
from utils.logging import main_logger
from utils.region_detection import RegionData
from utils.timing import TimeIt


class ReverseLogoRegionSearch(LogoFinder):
    """
    Finds logos within an image, and their URL origins,
    by reverse image searching detected logo regions in the image.
    """
    reverse_image_search_engines: list[ReverseImageSearchEngine] = None
    htmlsession: HTMLSession = None
    clf_logo: LogisticRegression = None

    _logger = main_logger.getChild('utils.reverse_image_search')
    worker = ThreadWorker()

    def __init__(
        self,
        reverse_image_search_engines: list[ReverseImageSearchEngine] = None,
        htmlsession: HTMLSession = None,
        clf: LogisticRegression = None
    ):
        super().__init__("reverse_logo_region_search")

        self.reverse_image_search_engines = reverse_image_search_engines
        self.htmlsession = htmlsession
        self.clf_logo = clf

    async def find(self, img_path: str, settings: DSTSettings) -> AsyncIterator[str]:
        self._logger.debug("Preparing for search info")

        # Finds the regions in the image
        with TimeIt('Region finding'):
            region_predictions = self.find_logo_probas(img_path)

        for revimg_search_engine in self.reverse_image_search_engines:
            # For each reverse image search engine, try to find the origin of each logo region
            with TimeIt(f'Logo reverse image search using {revimg_search_engine.name}'):
                try:
                    async for res in self.find_logo_origins(region_predictions, revimg_search_engine, settings):
                        yield res

                except Exception:
                    self._logger.error(f'Error while finding logo origins using search engine {revimg_search_engine.name}', exc_info=True)

    def find_logo_probas(self, img_path: str) -> list[tuple[RegionData, float]]:
        """
        Finds regions in an image, and calculates the probability of being a logo per region.

        Returns
        -------
        list[tuple[RegionData, float]]
            The list of logo-probability combinations: each region with their probability of being a logo.
        """

        # First, find the regions in the image
        regions, _ = region_detection.find_regions(img_path)
        self._logger.info(f"Found {len(regions)} regions")

        # Then, calculate the logo probability for each of the regions
        try:
            self._logger.debug(
                "(filepath, region, width, height, xcoord, ycoord, colourcount, dominant_colour_pct, parent, child, invert)"
            )

            region_probas = []
            for region_data in regions:
                # Submit region to the classifiers
                height, width, _ = region_data.region.shape
                self._logger.debug(
                    f"({region_data.index}, {width}, {height}, {region_data.x}, {region_data.y}, {region_data.unique_colors_count}, {region_data.pct}, {region_data.hierarchy[2]}, {region_data.hierarchy[3]})"
                )

                logo_prob = self.clf_logo.predict_proba([
                    [
                        width,
                        height,
                        region_data.x,
                        region_data.y,
                        region_data.unique_colors_count,
                        region_data.pct,
                        region_data.mean,
                        region_data.std,
                        region_data.skew,
                        region_data.kurtosis,
                        region_data.entropy,
                        region_data.otsu,
                        region_data.energy,
                        region_data.occupied_bins,
                    ]
                ])[:, 1][0]

                region_probas.append((region_data, logo_prob))

            return region_probas

        except Exception:
            self._logger.error(f'Exception while finding regions for img_path {img_path}', exc_info=True)

    def find_logo_origins(self, logo_probas: list[tuple[RegionData, float]], revimg_search_engine: ReverseImageSearchEngine, settings: DSTSettings) -> AsyncIterator[str]:
        """
        Find the origin of the 3 highest-logo-probability regions, using the given search engine.
        Uses async threads to concurrenlty find logo origin.
        """

        # Sort region predictions by logo probability, in descending order
        logo_probas.sort(key=lambda t: t[1], reverse=True)

        future_group = self.worker.new_future_group()

        region_count = 0
        for region_data, logo_proba in logo_probas:
            self._logger.info(f"Handling region {region_data.index}, with logo proba {logo_proba}")

            future_group.schedule([region_data], (lambda region_data: revimg_search_engine.query(region_data.region)))

            region_count += 1
            
            # Limit to the top `homebrew_regions` regions
            if region_count >= settings.homebrew_regions:
                break
            
        return stream.flatmap(future_group.generate(), lambda res: stream.iterate(res) | pipe.take(settings.homebrew_search_results))